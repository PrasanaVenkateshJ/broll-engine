"""Pick ONE clip per scene, with a rule anyone can check.

THE SELECTION RULE (its result and reason are written to timeline.json):
0. A statistic line (a number/percentage) gets a number card from `generated` (its text comes
   only from the narration, see cards.py)
   (switch off with GENERATED_FOR_STATISTICS=false).
1. Go through the sources in PROVIDER_ORDER (skipping any that are switched off).
2. Search the source with the scene's queries. Throw away candidates that
   - match an exclude term (safety / avoid list), or
   - were already used in this video (no clip id twice).
3. Score each candidate's preview picture with CLIP against the scene's meaning
   (average similarity to the 3 queries). Add small bonuses for a good length / orientation
   - the bonus only changes the ORDER, never whether a clip passes.
   Logo check (LOGO_CHECK, only for candidates that pass STOCK_MIN_SCORE): a calibrated LOGO
   PROBABILITY of the same picture = CLIP softmax over logo prompts (LOGO_PROMPTS: "a photo of a
   laptop with an apple logo", ...) and neutral prompts (LOGO_NEUTRAL_PROMPTS: "a photo of a
   laptop", ...), summed over the logo prompts; the highest of the whole picture and its
   LOGO_CROP_GRID x LOGO_CROP_GRID crops counts:
   - probability > LOGO_REJECT_PROB  -> rejected,
   - probability > LOGO_PENALTY_PROB -> kept, but LOGO_PENALTY is taken off its ORDER score, so a
     candidate without readable brand text wins a near tie.
   It does not compare against the scene score (that old margin rule rejected most real clips).
4. If the best CLIP score >= STOCK_MIN_SCORE: download the best candidate, check it with
   ffprobe; if it is corrupted, take the next passing one.
   Frame logo check: the thumbnail is only ONE picture, so LOGO_FRAME_COUNT frames of the
   DOWNLOADED clip get the same logo test; if any frame is above LOGO_REJECT_PROB, the clip is
   rejected (next one).
   Done. Otherwise (or if the source failed) go to the next source.
5. Nothing passed anywhere -> a `generated` motion card.

"Measurable" = the decision only depends on numbers we store (scores, threshold), so you
can re-check every choice from timeline.json.

SPEED (same choices, less waiting) - select_clips() works in two passes:
- pass 1, FOOTAGE_WORKERS scenes at the same time: search (the scene's queries at the same time),
  download previews (PREVIEW_WORKERS at a time, one shared connection pool), score them with CLIP
  in batches. The result is a Ranking per scene + source. Nothing is chosen here.
- pass 2, one scene at a time in scene order: the rule above picks the clip, so "no clip twice"
  works exactly as before and the result does not depend on thread timing.
The logo check (step 3) runs lazily inside pass 1/2: best candidates first, only until the winner
is certain - the same order as checking all of them. Searches and previews are cached per job and,
for FOOTAGE_CACHE_SECONDS, across jobs (footage_cache.py).
"""
import contextvars
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from app.config import get_settings
from app.pipeline import footage_cache, media
from app.pipeline.cards import build_card, check_card
from app.pipeline.net import DownloadError
from app.pipeline.providers import Candidate, ProviderError, get_provider, provider_status
from app.pipeline.providers.base import safe_name
from app.pipeline.providers.generated import GeneratedProvider
from app.pipeline.safety import SearchPlan, find_excluded
from app.pipeline.understand import SceneMeaning

log = logging.getLogger("app.pipeline.rank")

MAX_DOWNLOAD_TRIES = 3  # per source


# ---------- Scorers ----------

class Scorer:
    name = "base"
    needs_images = True

    def score(self, candidates: list[Candidate], previews: list[Path | None], texts: list[str]) -> list[float]:
        raise NotImplementedError

    def logo_probs(self, candidates: list[Candidate], previews: list[Path | None]) -> list[float]:
        """Probability 0..1 that each picture shows a visible logo / brand text / watermark."""
        raise NotImplementedError

    def frame_scores(self, candidate: Candidate, frames: list[Path], texts: list[str]) -> list[tuple[float, float]]:
        """(scene score, logo probability) for each frame of the downloaded clip."""
        same = [candidate] * len(frames)
        return list(zip(self.score(same, frames, texts), self.logo_probs(same, frames)))

    def score_images(self, images: list, texts: list[str]) -> list[float] | None:
        """Score raw RGB pictures (numpy arrays) against texts - used by the subject-aware crop.
        None = this scorer cannot look at pictures (render.py then uses the saliency crop)."""
        return None

    def info(self) -> dict:
        return {"scorer": self.name}


class MockScorer(Scorer):
    """No model: share of a query's words found in the clip's tags (0.15 .. 0.30).
    Logo probability: the candidate's extra["logo_prob"] if a test set one, else a low 0.05.
    Frame logo probabilities: extra["frame_logo_probs"] (one per frame) if a test set them, else
    the same value as the thumbnail."""
    name = "mock"
    needs_images = False

    def logo_probs(self, candidates, previews):
        return [float(c.extra.get("logo_prob", 0.05)) for c in candidates]

    def frame_scores(self, candidate, frames, texts):
        scene = self.score([candidate], [None], texts)[0]
        per_frame = candidate.extra.get("frame_logo_probs") or []
        default = float(candidate.extra.get("logo_prob", 0.05))
        return [(scene, float(per_frame[i]) if i < len(per_frame) else default) for i in range(len(frames))]

    def score(self, candidates, previews, texts):
        out = []
        for c in candidates:
            tags = set(c.tags)
            overlap = max((len(set(t.lower().split()) & tags) / max(1, len(t.split())) for t in texts), default=0.0)
            out.append(round(0.15 + 0.15 * overlap - 0.01 * int(c.extra.get("rank_in_query", 0)), 4))
        return out


class ClipScorer(Scorer):
    """open_clip (MIT): how well does the picture match the text? Cosine similarity, about 0.1-0.4.

    Speed: the model is loaded ONCE per worker process (class attribute; the worker loads it at start,
    CLIP_PRELOAD) and reused by every job. Pictures go through the model CLIP_BATCH_SIZE at a time.
    Text embeddings (queries, logo prompts) are kept, since the same texts come back for every candidate.
    Same result every time: a scene's pictures are always encoded together, in the same order, so the
    numbers never depend on which other scene happened to run first.
    """
    name = "clip"
    _lock = threading.RLock()  # re-entrant: score() holds it while helpers call _load()
    _model = None
    _texts: dict[tuple[str, ...], object] = {}  # (texts) -> normalised text embeddings, for this process

    @classmethod
    def _load(cls):
        with cls._lock:
            if cls._model is None:
                import open_clip
                import torch

                s = get_settings()
                model, _, preprocess = open_clip.create_model_and_transforms(s.CLIP_MODEL, pretrained=s.CLIP_PRETRAINED)
                model.eval()
                cls._model = (model, preprocess, open_clip.get_tokenizer(s.CLIP_MODEL), torch)
                cls._texts = {}
        return cls._model

    @classmethod
    def preload(cls) -> None:
        """Load the model now (the worker calls this at start, so the first job does not wait ~10 s)."""
        try:
            cls._load()
            log.info("CLIP model loaded")
        except Exception as e:  # the job will try again and report a real error if it still fails
            log.warning("CLIP preload failed: %s", e)

    def info(self):
        from importlib.metadata import version

        s = get_settings()
        return {"scorer": self.name, "library": "open_clip_torch", "version": version("open_clip_torch"),
                "model": s.CLIP_MODEL, "pretrained": s.CLIP_PRETRAINED}

    def _encode_texts(self, texts: list[str]):
        key = tuple(texts)
        if key not in self._texts:
            model, _, tokenizer, _ = self._load()
            text = model.encode_text(tokenizer(texts))
            self._texts[key] = text / text.norm(dim=-1, keepdim=True)
        return self._texts[key]

    def _prepare(self, pictures) -> list:
        """PIL pictures -> the model's input tensors (resize + normalise). Done OUTSIDE the lock, so
        one scene's thread can prepare its pictures while another scene's pictures are in the model."""
        preprocess = self._load()[1]
        return [preprocess(p) for p in pictures]

    def _encode_pictures(self, inputs: list):
        """Prepared inputs -> normalised CLIP embeddings, CLIP_BATCH_SIZE pictures per model call.
        Call with the lock held."""
        model, _, _, torch = self._load()
        size = max(1, get_settings().CLIP_BATCH_SIZE)
        parts = []
        for i in range(0, len(inputs), size):
            image = model.encode_image(torch.stack(inputs[i:i + size]))
            parts.append(image / image.norm(dim=-1, keepdim=True))
        return torch.cat(parts)

    def score(self, candidates, previews, texts):
        from PIL import Image

        torch = self._load()[3]
        inputs = self._prepare([Image.open(p).convert("RGB") for p in previews])
        with self._lock, torch.inference_mode():
            text = self._encode_texts([f"a photo of {t}" for t in texts])
            # Average cosine similarity of each picture to the queries = "how well it shows the meaning".
            sims = (self._encode_pictures(inputs) @ text.T).mean(dim=1)
        return [round(float(x), 4) for x in sims]

    def logo_probs(self, candidates, previews):
        """CLIP zero-shot classification "logo" vs "no logo" for each picture.

        For each VIEW of the picture (the whole picture + a LOGO_CROP_GRID x LOGO_CROP_GRID grid
        of crops, because a shop sign or a laptop logo is often a small part of the frame):
        softmax over all logo + neutral prompts (with CLIP's own logit scale, about 100), then the
        logo prompts' shares added up. The picture's logo probability = its highest view.
        """
        from PIL import Image

        s = get_settings()
        model, _, _, torch = self._load()
        logo, neutral = s.logo_prompts, s.logo_neutral_prompts
        if not previews:
            return []
        # All views of all pictures in batches (10 views per picture with the 3x3 grid).
        views = [logo_views(Image.open(path).convert("RGB"), s.LOGO_CROP_GRID) for path in previews]
        inputs = self._prepare([v for vs in views for v in vs])
        with self._lock, torch.inference_mode():
            text = self._encode_texts(logo + neutral)
            logits = model.logit_scale.exp() * (self._encode_pictures(inputs) @ text.T)
            per_view = logits.softmax(dim=-1)[:, :len(logo)].sum(dim=1)
        out, start = [], 0
        for vs in views:
            out.append(round(float(per_view[start:start + len(vs)].max()), 4))
            start += len(vs)
        return out

    def score_images(self, images, texts):
        from PIL import Image

        torch = self._load()[3]
        inputs = self._prepare([Image.fromarray(a) for a in images])
        with self._lock, torch.inference_mode():
            text = self._encode_texts([f"a photo of {t}" for t in texts])
            sims = (self._encode_pictures(inputs) @ text.T).mean(dim=1)
        return [round(float(x), 4) for x in sims]


def logo_views(picture, grid: int) -> list:
    """The whole PIL picture + a grid x grid set of crops (grid <= 1: the whole picture only)."""
    views = [picture]
    if grid > 1:
        w, h = picture.size
        for i in range(grid):
            for j in range(grid):
                views.append(picture.crop((w * i // grid, h * j // grid, w * (i + 1) // grid, h * (j + 1) // grid)))
    return views


def get_scorer() -> Scorer:
    return ClipScorer() if get_settings().RANK_SCORER == "clip" else MockScorer()


# ---------- Selection ----------

@dataclass
class Selection:
    scene_index: int
    candidate: Candidate
    path: Path | None          # downloaded file; None for a generated card
    clip_score: float | None   # CLIP (or mock) score; None for generated
    final_score: float | None  # clip_score + fit bonus
    reason: str
    query: str = ""
    # {"thumbnail": {logo score, gap, decision}, "frames": [one per clip frame], "result": ...};
    # None = not checked (generated card, or LOGO_CHECK=false)
    logo_check: dict | None = None

    def to_dict(self) -> dict:
        c = self.candidate
        return {"provider": c.provider, "clip_id": c.id, "uid": c.uid, "kind": c.kind, "query": self.query or c.query,
                "title": c.title, "page_url": c.page_url, "source_url": c.download_url,
                "licence": c.licence_name, "licence_url": c.licence_url, "attribution": c.attribution,
                "commercial_use": c.commercial_use, "watermark": c.watermark, "ai_generated": c.ai_generated,
                "generated": c.generated, "reason": self.reason, "relevance_score": self.clip_score,
                "final_score": self.final_score, "logo_check": self.logo_check, "width": c.width,
                "height": c.height, "duration": c.duration}

    def to_state(self) -> dict:
        """EVERYTHING about the choice (the full candidate too), so a clip swap can rebuild it later."""
        return {"candidate": self.candidate.to_dict(), "clip_score": self.clip_score, "final_score": self.final_score,
                "reason": self.reason, "query": self.query, "logo_check": self.logo_check}

    @classmethod
    def from_state(cls, scene_index: int, data: dict) -> "Selection":
        return cls(scene_index, Candidate(**data["candidate"]), None, data["clip_score"], data["final_score"],
                   data["reason"], data.get("query", ""), data.get("logo_check"))


@dataclass
class SelectionContext:
    """What all scenes of one job share. Pass 1 threads read and write it under `lock`."""
    work: Path
    orientation: str                           # landscape | portrait | square (first aspect ratio)
    warn: Callable[[str], None]
    scorer: Scorer
    used: set[str] = field(default_factory=set)            # clip uids already in this video
    cache: dict = field(default_factory=dict)              # search key -> results (or the ProviderError)
    previews: dict = field(default_factory=dict)           # candidate uid -> preview picture (None = failed)
    rankings: dict = field(default_factory=dict)           # (scene index, provider) -> Ranking
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


def fit_bonus(c: Candidate, scene_seconds: float, orientation: str) -> float:
    s = get_settings()
    bonus = 0.0
    if c.kind == "image" or (c.duration or 0) >= scene_seconds:
        bonus += s.DURATION_BONUS                    # long enough: no slow-down or loop needed
    elif (c.duration or 0) * (1 + s.MAX_SLOWDOWN) >= scene_seconds:
        bonus += s.DURATION_BONUS / 2                # needs a slight slow-down only
    if orientation == "square" or c.orientation == orientation:
        bonus += s.ORIENTATION_BONUS
    return round(bonus, 4)


def verify_media(path: Path, kind: str) -> str | None:
    """None if the file is a usable video/image, else what is wrong (ffprobe + a quick decode)."""
    info = media.probe(path)
    if not info:
        return "ffprobe cannot read it"
    streams = [st for st in info.get("streams", []) if st.get("codec_type") == "video"]
    if not streams:
        return "it has no picture"
    if int(streams[0].get("width") or 0) < 100 or int(streams[0].get("height") or 0) < 100:
        return "the picture is too small"
    if kind == "video":
        try:
            if float(info.get("format", {}).get("duration") or 0) < 0.5:
                return "it is shorter than 0.5 s"
        except ValueError:
            return "unknown length"
        try:  # ffprobe only reads the header; decoding 1 s catches broken data too
            media.ffmpeg("-v", "error", "-xerror", "-i", str(path), "-t", "1", "-f", "null", "-")
        except media.MediaError:
            return "the video data cannot be decoded"
    return None


def _pool_map(fn, items: list, workers: int) -> list:
    """fn(item) for every item on `workers` threads; results in the SAME order as items.
    Each thread gets a copy of the caller's context, so its log lines still carry the job_id."""
    items = list(items)
    if workers <= 1 or len(items) <= 1:
        return [fn(item) for item in items]
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as pool:
        futures = [pool.submit(contextvars.copy_context().run, fn, item) for item in items]
        return [f.result() for f in futures]


def _search(provider, queries: list[str], ctx: SelectionContext, scene_seconds: float):
    """All candidates for all queries (no duplicates, in query order), plus the error messages.

    The queries are sent at the same time. Each answer is kept for this job (ctx.cache) and, for real
    APIs, for FOOTAGE_CACHE_SECONDS for later jobs too (footage_cache). An error is kept for this job only.
    """
    s = get_settings()

    def one(query: str):
        key = (provider.name, query, ctx.orientation, round(scene_seconds, 2), s.PROVIDER_RESULTS_PER_QUERY)
        with footage_cache.single_flight(("search", id(ctx), key)):  # same query twice at once: ask once
            with ctx.lock:
                if key in ctx.cache:
                    return ctx.cache[key]
            result = footage_cache.get_search(key) if provider.cache_across_jobs else None
            if result is None:
                try:
                    result = provider.search(query, ctx.orientation, scene_seconds)
                    if provider.cache_across_jobs:
                        footage_cache.put_search(key, result)
                except ProviderError as e:
                    result = e
            with ctx.lock:
                ctx.cache[key] = result
            return result

    found: dict[str, Candidate] = {}
    errors: list[str] = []
    for result in _pool_map(one, queries, len(queries)):  # merged in query order: the same list every time
        if isinstance(result, ProviderError):
            errors.append(str(result))
            continue
        for c in result:
            found.setdefault(c.uid, c)
    return list(found.values()), errors


def _preview(provider, c: Candidate, ctx: SelectionContext) -> Path | None:
    """The candidate's preview picture: already loaded in this job, from the cache, or downloaded once."""
    with footage_cache.single_flight(("preview", id(ctx), c.uid)):
        with ctx.lock:
            if c.uid in ctx.previews:
                return ctx.previews[c.uid]
        path = None
        cached = (footage_cache.preview_file(c.preview_url) if provider.cache_across_jobs and c.preview_url
                  and get_settings().FOOTAGE_CACHE_SECONDS > 0 else None)
        try:
            if cached and footage_cache.fresh(cached):
                path = cached
            else:
                folder = ctx.work / "previews"
                folder.mkdir(parents=True, exist_ok=True)
                path = provider.fetch_preview(c, folder)
                if cached:
                    path = footage_cache.store_preview(path, c.preview_url)
        except (DownloadError, media.MediaError) as e:
            log.info("preview of %s failed: %s", c.uid, e)
        with ctx.lock:
            ctx.previews[c.uid] = path
        return path


def _score(provider, candidates: list[Candidate], texts: list[str], ctx: SelectionContext):
    """[(candidate, score, preview)] for the candidates whose preview could be loaded.
    The previews are downloaded PREVIEW_WORKERS at a time, then scored in one batch."""
    previews: list[Path | None] = [None] * len(candidates)
    if ctx.scorer.needs_images:
        previews = _pool_map(lambda c: _preview(provider, c, ctx), candidates, get_settings().PREVIEW_WORKERS)
        pairs = [(c, p) for c, p in zip(candidates, previews) if p is not None]
        candidates, previews = [c for c, _ in pairs], [p for _, p in pairs]
    if not candidates:
        return []
    try:
        scores = ctx.scorer.score(candidates, previews, texts)
    except Exception as e:  # a broken preview image must not fail the job
        log.warning("scoring failed: %s", e)
        return []
    return list(zip(candidates, scores, previews))


LOGO_BATCH = 4  # candidates logo-checked together (best first), see Ranking.ordered()


@dataclass
class Row:
    """One scored candidate in a Ranking."""
    candidate: Candidate
    score: float               # CLIP (or mock) score: decides whether it passes STOCK_MIN_SCORE
    bonus: float               # fit_bonus: only changes the order
    preview: Path | None
    position: int              # order in which the searches found it (a tie -> found first wins)
    checked: bool = False      # has the logo check run on it?
    logo: float | None = None  # its logo probability

    def decision(self) -> tuple[str, float, dict | None]:
        return logo_decision(self.logo) if self.checked else ("ok", 0.0, None)

    @property
    def best_possible(self) -> float:
        """Its order score if the logo check finds nothing (a logo can only make it lower)."""
        return round(self.score + self.bonus, 4)

    @property
    def final(self) -> float:
        return round(self.score + self.bonus - self.decision()[1], 4)


@dataclass
class Ranking:
    """Pass 1 for ONE scene and ONE provider: everything it found, scored.

    It does not depend on what other scenes chose (the no-repeat rule is applied later, in pass 2),
    so all scenes can build their rankings at the same time.
    """
    provider: str
    found: int               # candidates the searches returned
    usable_uids: list[str]   # ...minus those excluded by the safety / avoid list
    rows: list[Row]          # ...whose preview could be loaded and scored
    errors: list[str]

    def ordered(self, scorer: Scorer, check_logos: bool):
        """The rows that pass STOCK_MIN_SCORE and the logo check, best order score first.

        The logo check is the slow part (10 CLIP pictures per candidate), so it only runs as far as
        needed: candidates are checked LOGO_BATCH at a time, best first, and a checked one is handed
        out as soon as no unchecked one could still beat it. That gives exactly the order of checking
        every candidate first, with much less work. Results are kept on the rows, so a second call
        (a QA retry or a clip swap) never checks a candidate twice.
        """
        s = get_settings()
        pending = sorted((r for r in self.rows if r.score >= s.STOCK_MIN_SCORE),
                         key=lambda r: (-r.best_possible, r.position))
        ready: list[Row] = []
        i = 0
        while True:
            while i < len(pending) and (not ready or max((r.final, -r.position) for r in ready)
                                        < (pending[i].best_possible, -pending[i].position)):
                chunk = pending[i:i + LOGO_BATCH]
                todo = [r for r in chunk if not r.checked]
                if check_logos and todo:
                    _check_logos(todo, scorer)
                ready += [r for r in chunk if r.decision()[0] != "rejected"]
                i += len(chunk)
            if not ready:
                return
            best = max(ready, key=lambda r: (r.final, -r.position))
            ready.remove(best)
            yield best


def _check_logos(rows: list[Row], scorer: Scorer) -> None:
    try:
        probs = scorer.logo_probs([r.candidate for r in rows], [r.preview for r in rows])
    except Exception as e:  # cannot check = do not trust it: treated as showing a logo
        log.warning("logo check failed: %s", e)
        probs = [1.0] * len(rows)
    for r, p in zip(rows, probs):
        r.logo, r.checked = p, True


def get_ranking(provider, scene, plan: SearchPlan, ctx: SelectionContext) -> Ranking:
    """The scene's Ranking for this provider (made once per job, then reused)."""
    key = (scene.index, provider.name)
    with footage_cache.single_flight(("ranking", id(ctx), key)):
        with ctx.lock:
            if key in ctx.rankings:
                return ctx.rankings[key]
        candidates, errors = _search(provider, plan.queries, ctx, scene.duration)
        usable = [c for c in candidates if not find_excluded(c.search_text(), plan.exclude_terms)]
        scored = _score(provider, usable, plan.queries, ctx)
        rows = [Row(c, score, fit_bonus(c, scene.duration, ctx.orientation), preview, i)
                for i, (c, score, preview) in enumerate(scored)]
        ranking = Ranking(provider.name, len(candidates), [c.uid for c in usable], rows, errors)
        with ctx.lock:
            ctx.rankings[key] = ranking
        return ranking


def logo_decision(logo_prob: float | None) -> tuple[str, float, dict | None]:
    """("ok" | "penalised" | "rejected", order-score penalty, facts for timeline.json)."""
    s = get_settings()
    if logo_prob is None:
        return "ok", 0.0, None
    if logo_prob > s.LOGO_REJECT_PROB:
        result, penalty = "rejected", 0.0
    elif logo_prob > s.LOGO_PENALTY_PROB:
        result, penalty = "penalised", s.LOGO_PENALTY
    else:
        result, penalty = "ok", 0.0
    return result, penalty, {"logo_prob": logo_prob, "result": result, "reject_prob": s.LOGO_REJECT_PROB,
                             "penalty_prob": s.LOGO_PENALTY_PROB, "penalty": penalty}


def grab_check_frames(path: Path, kind: str, folder: Path, uid: str, count: int) -> list[tuple[float | None, Path]]:
    """`count` frames spread over the WHOLE downloaded clip, as JPEGs: [(time in seconds, path)].
    A still image has only one picture: [(None, the image itself)]."""
    if kind == "image":
        return [(None, path)]
    duration = media.duration_of(path)
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(max(1, count)):
        t = round(duration * (i + 0.5) / max(1, count), 2)  # 1/6, 1/2, 5/6 of the clip for 3 frames
        dest = folder / f"{safe_name(uid)}_f{i}.jpg"
        media.ffmpeg("-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1", "-vf", "scale=640:-2", str(dest))
        if dest.is_file():
            out.append((t, dest))
    if not out:
        raise media.MediaError("no frames could be taken from the clip")
    return out


def frame_logo_check(candidate: Candidate, path: Path, texts: list[str], scorer: Scorer,
                     folder: Path) -> tuple[bool, list[dict]]:
    """Run the logo test on frames of the downloaded clip. Returns (rejected?, facts per frame).
    The same rule as the thumbnail: logo probability > LOGO_REJECT_PROB -> rejected."""
    s = get_settings()
    frames = grab_check_frames(path, candidate.kind, folder, candidate.uid, s.LOGO_FRAME_COUNT)
    scores = scorer.frame_scores(candidate, [p for _, p in frames], texts)
    facts = []
    for (t, _), (scene, logo) in zip(frames, scores):
        result, _, _ = logo_decision(logo)
        facts.append({"time": t, "logo_prob": logo, "scene_score": scene, "result": result})
    return any(f["result"] == "rejected" for f in facts), facts


def _card(scene, meaning: SceneMeaning, warn: Callable[[str], None] | None = None) -> Candidate:
    """The generated card for a scene. Its text comes from the narration only (cards.build_card), and
    is checked once more right here: a number the scene's narration does not say is never drawn."""
    card = meaning.card or build_card(scene.text, meaning.keyword, meaning.names).to_dict()
    headline, subtitle = card["headline"], card.get("subtitle", "")
    bad = check_card(headline, subtitle, scene.text)
    if bad:  # cannot happen with build_card; if it ever does, show the keyword without any number
        (warn or log.warning)(f"Scene {scene.index}: card text '{headline} {subtitle}' has {', '.join(bad)}, "
                              "which the narration does not say; showed a keyword card instead")
        card = build_card("", meaning.keyword).to_dict()
        headline, subtitle = card["headline"], ""
    cand = GeneratedProvider().card(scene.index, headline, subtitle)
    cand.extra.update(card_source="narration", card_kind=card.get("kind", "keyword"),
                      numbers_shown=card.get("numbers_shown", []), subtitle_from=card.get("subtitle_from", "narration"))
    return cand


def _statistic_card_line(meaning: SceneMeaning) -> bool:
    """Step 0 of the rule: does this line go straight to a number card?"""
    return bool(meaning.flags.get("statistic") and meaning.number and get_settings().GENERATED_FOR_STATISTICS
                and provider_status("generated") is None)


def _logo_notes(name: str, rows: list[Row]) -> list[str]:
    """ "pixabay_video: logo check rejected 2, best: ..." (best-scoring rejected ones first)."""
    rejected = sorted((r for r in rows if r.decision()[0] == "rejected"), key=lambda r: (-r.score, r.position))
    if not rejected:
        return []
    top = [f"{r.candidate.uid} (logo prob {r.logo:.2f}, scene {r.score:.3f})" for r in rejected[:3]]
    checked = sum(r.checked for r in rows)  # the check stops once the winner is certain (Ranking.ordered)
    return [f"{name}: logo check rejected {len(rejected)}, best: " + ", ".join(top) + f" ({checked} checked)"]


def select_clip(scene, meaning: SceneMeaning, plan: SearchPlan, ctx: SelectionContext) -> Selection:
    """Apply the selection rule for one scene (pass 2). Never raises for provider trouble.

    Uses the scene's Rankings from pass 1 (or makes them now, e.g. for a clip swap).
    ctx.used holds every clip already chosen in this video - including one that failed a
    quality check - so it is never picked again.
    """
    s = get_settings()
    notes: list[str] = []

    if _statistic_card_line(meaning):
        card = _card(scene, meaning, ctx.warn)
        if card.uid not in ctx.used:
            ctx.used.add(card.uid)
            return Selection(scene.index, card, None, None, None,
                             f"statistic line: number card '{card.extra['headline']}' taken from the narration "
                             "(GENERATED_FOR_STATISTICS=true)")

    for name in s.provider_order:
        if name == "generated":
            continue  # the last resort, below
        status = provider_status(name)  # checked per scene: a provider can be switched off mid-job
        if status:
            notes.append(f"{name} skipped ({status})")
            continue
        provider = get_provider(name)
        ranking = get_ranking(provider, scene, plan, ctx)
        if ranking.errors:
            ctx.warn(f"Footage source {name} failed ({ranking.errors[0]}); used the next source")
            if not ranking.found:
                notes.append(f"{name} failed ({ranking.errors[0]})")
                continue
        if not [uid for uid in ranking.usable_uids if uid not in ctx.used]:
            notes.append(f"{name}: no usable results ({ranking.found} found, all excluded or already used)")
            continue
        rows = [r for r in ranking.rows if r.candidate.uid not in ctx.used]
        if not rows:
            notes.append(f"{name}: previews could not be scored")
            continue
        tries, download_notes = 0, []
        for row in ranking.ordered(ctx.scorer, s.LOGO_CHECK):
            cand, score, final = row.candidate, row.score, row.final
            if cand.uid in ctx.used:
                continue
            if tries >= MAX_DOWNLOAD_TRIES:
                break
            tries += 1
            logo_facts = row.decision()[2]
            folder = ctx.work / "clips"
            folder.mkdir(parents=True, exist_ok=True)
            try:
                path = provider.fetch(cand, folder)
            except (DownloadError, media.MediaError) as e:
                download_notes.append(f"{cand.uid} download failed ({e})")
                continue
            problem = verify_media(path, cand.kind)
            if problem:
                ctx.warn(f"Scene {scene.index}: downloaded clip {cand.uid} is corrupted ({problem}); "
                         "tried the next candidate")
                download_notes.append(f"{cand.uid} corrupted ({problem})")
                continue
            logo_report, frame_note = None, ""
            if s.LOGO_CHECK:
                try:
                    rejected, frame_facts = frame_logo_check(cand, path, plan.queries, ctx.scorer, ctx.work / "frames")
                except Exception as e:  # frames could not be scored: the thumbnail check still stands
                    log.warning("frame logo check of %s failed: %s", cand.uid, e)
                    rejected, frame_facts, frame_note = False, [], f"; frame check not possible ({e})"
                if rejected:
                    bad = next(f for f in frame_facts if f["result"] == "rejected")
                    where = f"at {bad['time']:.1f} s" if bad["time"] is not None else "in the picture"
                    ctx.used.add(cand.uid)  # never try it again in this video
                    ctx.warn(f"Scene {scene.index}: clip {cand.uid} shows a logo {where} of the downloaded clip "
                             f"(logo prob {bad['logo_prob']:.2f} > {s.LOGO_REJECT_PROB:.2f}); "
                             "tried the next candidate")
                    download_notes.append(f"{cand.uid} rejected by the frame logo check ({where}, "
                                          f"logo prob {bad['logo_prob']:.2f})")
                    continue
                logo_report = {"thumbnail": logo_facts, "frames": frame_facts,
                               "result": (logo_facts or {}).get("result", "ok")}
                if frame_facts:
                    frame_note = (f"; {len(frame_facts)} clip frame(s) passed the logo check "
                                  f"(max logo prob {max(f['logo_prob'] for f in frame_facts):.2f})")
            ctx.used.add(cand.uid)
            reason = (f"{name}: score {score:.3f} >= {s.STOCK_MIN_SCORE:.2f} for query '{cand.query}' "
                      f"(best of {len(rows)} candidates, order bonus {final - score:+.2f})")
            if logo_facts:
                reason += f"; logo check {logo_facts['result']} (logo prob {logo_facts['logo_prob']:.2f})"
            reason += frame_note
            earlier = notes + _logo_notes(name, rows) + download_notes
            if earlier:
                reason += "; earlier: " + "; ".join(earlier)
            return Selection(scene.index, cand, path, score, round(final, 4), reason, cand.query, logo_report)
        notes += _logo_notes(name, rows)
        if tries == 0:  # nothing passed the threshold (after the logo check)
            kept = [r for r in rows if r.decision()[0] != "rejected"]
            if kept:
                notes.append(f"{name}: best score {max(r.score for r in kept):.3f} < {s.STOCK_MIN_SCORE:.2f}")
            continue
        notes += download_notes
        notes.append(f"{name}: no passing candidate could be downloaded")

    card = _card(scene, meaning, ctx.warn)
    ctx.used.add(card.uid)
    return Selection(scene.index, card, None, None, None,
                     f"no stock source passed the rule ({'; '.join(notes) or 'no stock sources active'}) "
                     f"-> generated motion card '{card.extra['headline']}'")


def prepare_rankings(scenes: list, meanings: list[SceneMeaning], plans: list[SearchPlan],
                     ctx: SelectionContext) -> None:
    """Pass 1: search + preview + score footage for FOOTAGE_WORKERS scenes at the same time.

    For each scene it walks the sources in PROVIDER_ORDER until one has a candidate that passes the
    threshold and the logo check (that source will very likely be chosen). Nothing is CHOSEN here,
    so the no-repeat rule cannot be broken by two threads. A problem only means pass 2 does that
    work itself.
    """
    s = get_settings()

    def prepare(i: int) -> None:
        if _statistic_card_line(meanings[i]):
            return  # goes straight to a number card: no footage needed
        try:
            for name in s.provider_order:
                if name == "generated" or provider_status(name):
                    continue
                ranking = get_ranking(get_provider(name), scenes[i], plans[i], ctx)
                if next(ranking.ordered(ctx.scorer, s.LOGO_CHECK), None) is not None:
                    return
        except Exception as e:  # pass 2 tries again and handles it the normal way
            log.warning("footage pass 1 for scene %d failed: %s", scenes[i].index, e)

    _pool_map(prepare, list(range(len(scenes))), s.FOOTAGE_WORKERS)


def select_clips(scenes: list, meanings: list[SceneMeaning], plans: list[SearchPlan], ctx: SelectionContext,
                 on_scene_done: Callable[[int], None] | None = None) -> tuple[list[Selection], dict]:
    """One clip per scene, in two passes:

    pass 1 (parallel): every scene searches, downloads previews and scores them - the slow part;
    pass 2 (one scene at a time, in scene order): the selection rule picks, downloads and checks the
    clip, honouring the no-repeat rule exactly as before.
    Same inputs -> same choices, whatever FOOTAGE_WORKERS is. Returns (selections, timing facts).
    """
    footage_cache.prune()
    t0 = time.perf_counter()
    prepare_rankings(scenes, meanings, plans, ctx)
    t1 = time.perf_counter()
    selections = []
    for i, (scene, meaning, plan) in enumerate(zip(scenes, meanings, plans)):
        selections.append(select_clip(scene, meaning, plan, ctx))
        if on_scene_done:
            on_scene_done(i)
    facts = {"footage_workers": max(1, get_settings().FOOTAGE_WORKERS),
             "pass1_search_score_seconds": round(t1 - t0, 3),
             "pass2_select_download_seconds": round(time.perf_counter() - t1, 3)}
    return selections, facts
