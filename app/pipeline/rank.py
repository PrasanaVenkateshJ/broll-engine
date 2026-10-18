"""Pick ONE clip per scene, with a rule anyone can check.

THE SELECTION RULE (its result and reason are written to timeline.json):
0. A statistic line (a number/percentage): its number is always SHOWN, text only from the narration
   (cards.py). STATISTIC_OVERLAY=true: the rule below looks for real footage of the line's topic and the
   number is drawn on it as a badge ("overlay": true); only if nothing passes, a plain number card.
   STATISTIC_OVERLAY=false: straight to a number card from `generated` (the old behaviour).
1. Go through the sources in PROVIDER_ORDER (skipping any that are switched off or cooling down after
   an HTTP 429).
2. Search the source with the scene's queries (up to QUERIES_PER_SCENE: meaning, metaphor, setting, action,
   mood; PROVIDER_MAX_QUERIES caps small-quota APIs), results merged round-robin over the queries, at most
   MAX_CANDIDATES_PER_SOURCE scored. EARLY_EXIT: they are scored EARLY_EXIT_STEP at a time and scoring stops
   as soon as one clearly passes (>= STOCK_MIN_SCORE + PREFER_VIDEO_MARGIN, logo check ok); the rest is scored
   only if pass 2 runs out of usable candidates of that source. Throw away candidates that
   - match an exclude term (safety / avoid list), or
   - were already used in this video (no clip id twice).
3. Score each candidate with CLIP against the scene's meaning:
   - prompt ensembling: each query is encoded as CLIP_PROMPTS ("a stock video of {q}", "a photo of {q}",
     "{q}") and the text embeddings are averaged;
   - score = mean of the candidate's best SCORE_TOP_QUERIES query similarities;
   - more frames: the best MULTI_FRAME_TOP_K video candidates (by thumbnail) also get 2 frames (30% / 70%)
     of their ~1 MB smallest rendition; their score = mean over thumbnail + frames.
   Add small bonuses for a good length / orientation and take penalties for readable logos (LOGO_*) and
   text-heavy pictures (TEXT_*) - bonuses and penalties only change the ORDER, never whether a clip passes.
   Logo check (LOGO_CHECK, only for candidates that pass the threshold): a calibrated LOGO PROBABILITY of
   the same picture = CLIP softmax over logo prompts vs neutral prompts, summed over the logo prompts; the
   highest of the whole picture and its LOGO_CROP_GRID x LOGO_CROP_GRID crops counts:
   - probability > LOGO_REJECT_PROB  -> rejected,
   - probability > LOGO_PENALTY_PROB -> kept, but LOGO_PENALTY is taken off its ORDER score.
4. Independent judge (VLM_PROVIDER, optional, see vlm.py): for up to VLM_MAX_SCENES_PER_JOB scenes (the
   most uncertain first; confident scenes only if VLM_JUDGE_CONFIDENT) a vision-language model rates the top
   VLM_TOP_K candidates 1-5. Among the judged
   candidates of a source the higher rating goes first; a rating <= VLM_REJECT_SCORE drops it.
5. If the best CLIP score >= STOCK_MIN_SCORE: download the best candidate, check it with ffprobe; if it is
   corrupted, take the next passing one. Frame logo check on LOGO_FRAME_COUNT frames of the DOWNLOADED clip.
   Video first: a passing stock VIDEO scoring below STOCK_MIN_SCORE + PREFER_VIDEO_MARGIN is compared with
   the next photo-motion source; the photo wins only if its order score is more than PREFER_VIDEO_MARGIN
   higher. A video at or above STOCK_MIN_SCORE + PREFER_VIDEO_MARGIN is taken without searching photos.
   Done. Otherwise (or if the source failed) go to the next source.
6. Adaptive threshold: nothing passed anywhere -> the candidates scoring at least
   STOCK_MIN_SCORE x (1 - RELATIVE_MARGIN) may still be used, but ONLY one the judge rated
   VLM_ACCEPT_SCORE or more (best rating first). No judge -> no adaptive pass.
7. Still nothing -> a `generated` motion card.

"Measurable" = the decision only depends on numbers we store (scores, frames, ratings, thresholds), so you
can re-check every choice from timeline.json.

QUALITY_MODE (config.py) picks how hard this tries: fast (3 queries, 10 candidates, no extra frames, no judge),
balanced (5 queries, 15 candidates, early exit, judge only close calls / near misses) or best (5 queries x 2
pages, 48 candidates, no early exit, judge confident scenes too). The rule itself is the same in every mode.

SPEED (same choices, less waiting) - select_clips() works in passes:
- pass 1, FOOTAGE_WORKERS scenes at the same time: search, download previews, score them with CLIP in
  batches (+ extra frames for the best few). The result is a Ranking per scene + source. Nothing is chosen.
- pass 1.5 (only with a judge): the most uncertain scenes are judged, one request at a time.
- pass 2, one scene at a time in scene order: the rule above picks the clip, so "no clip twice" works
  exactly as before and the result does not depend on thread timing.
PREFETCH_CLIPS: pass 1 already downloads + verifies + frame-logo-checks each scene's likely winner (in
parallel); pass 2 reuses those facts when it picks that clip (a different pick just downloads as before).
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
from app.pipeline.vlm import JudgeError, JudgeUnavailable

log = logging.getLogger("app.pipeline.rank")

MAX_DOWNLOAD_TRIES = 3  # per source


def adaptive_floor() -> float:
    """The lowest CLIP score the adaptive pass may accept: STOCK_MIN_SCORE x (1 - RELATIVE_MARGIN)."""
    s = get_settings()
    return round(s.STOCK_MIN_SCORE * (1 - s.RELATIVE_MARGIN), 4)


# ---------- Scorers ----------

class Scorer:
    name = "base"
    needs_images = True

    def score(self, candidates: list[Candidate], previews: list[Path | None], texts: list[str]) -> list[float]:
        raise NotImplementedError

    def score_details(self, candidates, previews, texts) -> tuple[list[float], list[float]]:
        """(scene scores, text-heavy probabilities 0..1) - one look at each picture for both."""
        return self.score(candidates, previews, texts), [0.0] * len(candidates)

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
    the same value as the thumbnail. Text-heavy probability: extra["text_prob"], else 0.
    A test can fix a candidate's score with extra["score"]."""
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
            if "score" in c.extra:
                out.append(float(c.extra["score"]))
                continue
            tags = set(c.tags)
            overlap = max((len(set(t.lower().split()) & tags) / max(1, len(t.split())) for t in texts), default=0.0)
            out.append(round(0.15 + 0.15 * overlap - 0.01 * int(c.extra.get("rank_in_query", 0)), 4))
        return out

    def score_details(self, candidates, previews, texts):
        return self.score(candidates, previews, texts), [float(c.extra.get("text_prob", 0.0)) for c in candidates]


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
                "model": s.CLIP_MODEL, "pretrained": s.CLIP_PRETRAINED, "prompts": s.clip_prompts,
                "score": f"mean of the best {s.SCORE_TOP_QUERIES} query similarities (prompt-ensembled)",
                "multi_frame_top_k": s.MULTI_FRAME_TOP_K, "multi_frame_positions": s.multi_frame_positions}

    def _encode_texts(self, texts: list[str]):
        key = tuple(texts)
        if key not in self._texts:
            model, _, tokenizer, _ = self._load()
            text = model.encode_text(tokenizer(texts))
            self._texts[key] = text / text.norm(dim=-1, keepdim=True)
        return self._texts[key]

    def _query_embeddings(self, queries: list[str]):
        """One embedding per query: the average of its CLIP_PROMPTS versions (prompt ensembling), normalised."""
        prompts = get_settings().clip_prompts
        key = ("ensemble", *prompts, "|", *queries)
        if key not in self._texts:
            flat = self._encode_texts([p.replace("{q}", q) for q in queries for p in prompts])
            mean = flat.reshape(len(queries), len(prompts), -1).mean(dim=1)
            self._texts[key] = mean / mean.norm(dim=-1, keepdim=True)
        return self._texts[key]

    def _scene_scores(self, image_embeddings, texts: list[str]):
        """Per picture: the mean of its best SCORE_TOP_QUERIES query similarities."""
        sims = image_embeddings @ self._query_embeddings(texts).T
        k = max(1, min(get_settings().SCORE_TOP_QUERIES, sims.shape[1]))
        return sims.topk(k, dim=1).values.mean(dim=1)

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
        return self.score_details(candidates, previews, texts)[0]

    def score_details(self, candidates, previews, texts):
        from PIL import Image

        s = get_settings()
        model, _, _, torch = self._load()
        inputs = self._prepare([Image.open(p).convert("RGB") for p in previews])
        with self._lock, torch.inference_mode():
            images = self._encode_pictures(inputs)
            scores = self._scene_scores(images, texts)
            text_prompts, neutral = s.text_prompts, s.logo_neutral_prompts
            if text_prompts:
                logits = model.logit_scale.exp() * (images @ self._encode_texts(text_prompts + neutral).T)
                text_probs = logits.softmax(dim=-1)[:, :len(text_prompts)].sum(dim=1)
            else:
                text_probs = torch.zeros(len(previews))
        return [round(float(x), 4) for x in scores], [round(float(x), 4) for x in text_probs]

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
            sims = self._scene_scores(self._encode_pictures(inputs), texts)
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
    final_score: float | None  # clip_score + fit bonus - penalties
    reason: str
    query: str = ""
    # {"thumbnail": {logo score, gap, decision}, "frames": [one per clip frame], "result": ...};
    # None = not checked (generated card, or LOGO_CHECK=false)
    logo_check: dict | None = None
    # Statistic line on real footage: the number badge drawn over the clip (text from the narration only).
    overlay: dict | None = None
    # The independent judge's rating of this clip and of the other judged candidates (None = not judged).
    vlm: dict | None = None
    # How the score was made: thumbnail score, extra frame scores, text-heavy probability, penalties.
    details: dict | None = None

    def to_dict(self) -> dict:
        c = self.candidate
        return {"provider": c.provider, "clip_id": c.id, "uid": c.uid, "kind": c.kind, "query": self.query or c.query,
                "title": c.title, "page_url": c.page_url, "source_url": c.download_url,
                "licence": c.licence_name, "licence_url": c.licence_url, "attribution": c.attribution,
                "commercial_use": c.commercial_use, "watermark": c.watermark, "ai_generated": c.ai_generated,
                "generated": c.generated, "reason": self.reason, "relevance_score": self.clip_score,
                "final_score": self.final_score, "logo_check": self.logo_check, "width": c.width,
                "height": c.height, "duration": c.duration, "overlay": self.overlay, "vlm": self.vlm,
                "score_details": self.details}

    def to_state(self) -> dict:
        """EVERYTHING about the choice (the full candidate too), so a clip swap can rebuild it later."""
        return {"candidate": self.candidate.to_dict(), "clip_score": self.clip_score, "final_score": self.final_score,
                "reason": self.reason, "query": self.query, "logo_check": self.logo_check, "overlay": self.overlay,
                "vlm": self.vlm, "details": self.details}

    @classmethod
    def from_state(cls, scene_index: int, data: dict) -> "Selection":
        return cls(scene_index, Candidate(**data["candidate"]), None, data["clip_score"], data["final_score"],
                   data["reason"], data.get("query", ""), data.get("logo_check"), data.get("overlay"),
                   data.get("vlm"), data.get("details"))


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
    judge: object | None = None                            # vlm.Judge or None (VLM_PROVIDER=none)
    vlm: dict = field(default_factory=dict)                # scene index -> what the judge said (pass 1.5)
    prefetched: dict = field(default_factory=dict)         # uid -> downloaded + checked in pass 1 (see _prefetch)


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
        key = (provider.name, query, ctx.orientation, round(scene_seconds, 2), s.PROVIDER_RESULTS_PER_QUERY,
               s.PROVIDER_PAGES)
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
    lists = []
    for result in _pool_map(one, queries, len(queries)):  # merged in a fixed order: the same list every time
        if isinstance(result, ProviderError):
            errors.append(str(result))
            continue
        lists.append(list(result))
    # Round-robin over the queries: every query's 1st result, then every query's 2nd, ... (no duplicates).
    for rank in range(max((len(r) for r in lists), default=0)):
        for result in lists:
            if rank < len(result):
                found.setdefault(result[rank].uid, result[rank])
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
    """[(candidate, score, text-heavy probability, preview)] for the candidates whose preview could be loaded.
    The previews are downloaded PREVIEW_WORKERS at a time, then scored in one batch."""
    previews: list[Path | None] = [None] * len(candidates)
    if ctx.scorer.needs_images:
        previews = _pool_map(lambda c: _preview(provider, c, ctx), candidates, get_settings().PREVIEW_WORKERS)
        pairs = [(c, p) for c, p in zip(candidates, previews) if p is not None]
        candidates, previews = [c for c, _ in pairs], [p for _, p in pairs]
    if not candidates:
        return []
    try:
        scores, text_probs = ctx.scorer.score_details(candidates, previews, texts)
    except Exception as e:  # a broken preview image must not fail the job
        log.warning("scoring failed: %s", e)
        return []
    return list(zip(candidates, scores, text_probs, previews))


LOGO_BATCH = 2  # candidates logo-checked together (best first), see Ranking.ordered()


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
    text_prob: float = 0.0     # how text-heavy the picture looks (0..1)
    thumb_score: float | None = None        # the thumbnail-only score, when extra frames changed `score`
    frame_scores: list[float] | None = None  # [thumbnail, frame at 30 %, frame at 70 %]

    def decision(self) -> tuple[str, float, dict | None]:
        return logo_decision(self.logo) if self.checked else ("ok", 0.0, None)

    @property
    def text_penalty(self) -> float:
        s = get_settings()
        return s.TEXT_PENALTY if self.text_prob > s.TEXT_PENALTY_PROB else 0.0

    @property
    def best_possible(self) -> float:
        """Its order score if the logo check finds nothing (a logo can only make it lower)."""
        return round(self.score + self.bonus - self.text_penalty, 4)

    @property
    def final(self) -> float:
        return round(self.score + self.bonus - self.text_penalty - self.decision()[1], 4)

    def details(self) -> dict:
        s = get_settings()
        out = {"score": self.score, "thumbnail_score": self.thumb_score if self.thumb_score is not None else self.score,
               "frame_scores": self.frame_scores, "frames_scored": len(self.frame_scores or [None]),
               "text_prob": self.text_prob, "text_penalty": self.text_penalty, "fit_bonus": self.bonus,
               "logo_penalty": self.decision()[1], "order_score": self.final}
        if self.frame_scores:
            out["frames_note"] = (f"score = mean of the thumbnail and {len(self.frame_scores) - 1} frame(s) at "
                                  f"{', '.join(f'{p:.0%}' for p in s.multi_frame_positions)} of the clip")
        return out


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
    media_kind: str = "video"
    rest: list[Candidate] = field(default_factory=list)  # not scored yet: early exit stopped before them
    stopped_after: int | None = None                     # candidates scored when early exit stopped

    def _ordered(self, scorer: Scorer, check_logos: bool, min_score: float):
        """The rows that pass `min_score` and the logo check, best order score first.

        The logo check is the slow part (10 CLIP pictures per candidate), so it only runs as far as
        needed: candidates are checked LOGO_BATCH at a time, best first, and a checked one is handed
        out as soon as no unchecked one could still beat it. That gives exactly the order of checking
        every candidate first, with much less work. Results are kept on the rows, so a second call
        (a QA retry or a clip swap) never checks a candidate twice.
        """
        pending = sorted((r for r in self.rows if r.score >= min_score), key=lambda r: (-r.best_possible, r.position))
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

    def ordered(self, scorer: Scorer, check_logos: bool, min_score: float | None = None,
                vlm_scores: dict[str, int] | None = None):
        """Passing rows in selection order. With judge ratings (vlm_scores): the judged rows come first,
        higher rating first (CLIP order breaks ties); a rating <= VLM_REJECT_SCORE drops the row."""
        s = get_settings()
        min_score = s.STOCK_MIN_SCORE if min_score is None else min_score
        base = self._ordered(scorer, check_logos, min_score)
        judged = {r.candidate.uid for r in self.rows if r.candidate.uid in (vlm_scores or {}) and r.score >= min_score}
        if not judged:
            yield from base
            return
        head, held, seen = [], [], set()
        for row in base:
            uid = row.candidate.uid
            if uid in judged:
                seen.add(uid)
                if vlm_scores[uid] > s.VLM_REJECT_SCORE:
                    head.append(row)
            else:
                held.append(row)
            if judged <= seen:
                break
        head.sort(key=lambda r: (-vlm_scores[r.candidate.uid], -r.final, r.position))
        yield from head
        yield from held
        yield from base


def _check_logos(rows: list[Row], scorer: Scorer) -> None:
    try:
        probs = scorer.logo_probs([r.candidate for r in rows], [r.preview for r in rows])
    except Exception as e:  # cannot check = do not trust it: treated as showing a logo
        log.warning("logo check failed: %s", e)
        probs = [1.0] * len(rows)
    for r, p in zip(rows, probs):
        r.logo, r.checked = p, True


def _multi_frame(provider, rows: list[Row], texts: list[str], ctx: SelectionContext) -> None:
    """The best MULTI_FRAME_TOP_K video rows (by thumbnail score, at least the adaptive floor) get extra
    frames from their small rendition; their score becomes the mean over thumbnail + frames."""
    s = get_settings()
    positions = s.multi_frame_positions
    if s.MULTI_FRAME_TOP_K <= 0 or not positions or not ctx.scorer.needs_images:
        return
    floor = adaptive_floor()
    pick = sorted((r for r in rows if r.candidate.kind == "video" and r.score >= floor),
                  key=lambda r: (-r.score, r.position))[:s.MULTI_FRAME_TOP_K]

    def frames_of(row: Row) -> list[Path]:
        try:
            return provider.probe_frames(row.candidate, ctx.work / "probe", positions)
        except (DownloadError, media.MediaError, ProviderError) as e:
            log.info("extra frames of %s failed: %s", row.candidate.uid, e)
            return []

    for row, frames in zip(pick, _pool_map(frames_of, pick, min(len(pick), s.PREVIEW_WORKERS))):
        if not frames:
            continue
        try:
            extra = ctx.scorer.score([row.candidate] * len(frames), frames, texts)
        except Exception as e:  # keep the thumbnail score
            log.warning("scoring extra frames of %s failed: %s", row.candidate.uid, e)
            continue
        row.thumb_score = row.score
        row.frame_scores = [row.score, *extra]
        row.score = round(sum(row.frame_scores) / len(row.frame_scores), 4)


def get_ranking(provider, scene, plan: SearchPlan, ctx: SelectionContext) -> Ranking:
    """The scene's Ranking for this provider (made once per job, then reused)."""
    key = (scene.index, provider.name)
    with footage_cache.single_flight(("ranking", id(ctx), key)):
        with ctx.lock:
            if key in ctx.rankings:
                return ctx.rankings[key]
        s = get_settings()
        candidates, errors = _search(provider, provider.max_queries(plan.queries), ctx, scene.duration)
        usable = [c for c in candidates if not find_excluded(c.search_text(), plan.exclude_terms)]
        cap = s.MAX_CANDIDATES_PER_SOURCE
        pool = usable[:cap] if cap > 0 else usable
        step = max(1, s.EARLY_EXIT_STEP) if s.EARLY_EXIT else max(1, len(pool))
        clear = s.STOCK_MIN_SCORE + s.PREFER_VIDEO_MARGIN
        rows: list[Row] = []
        stopped = None
        for start in range(0, len(pool), step):
            rows += _rows(_score(provider, pool[start:start + step], plan.queries, ctx), scene, ctx, len(rows))
            if start + step >= len(pool):
                break
            top = [r for r in rows if r.score >= clear and not r.checked]
            if s.LOGO_CHECK and top:
                _check_logos(top, ctx.scorer)
            if any(r.score >= clear and r.decision()[0] != "rejected" for r in rows):
                stopped = start + step  # a clear winner: the rest is scored only if pass 2 needs it
                break
        _multi_frame(provider, rows, plan.queries, ctx)
        ranking = Ranking(provider.name, len(candidates), [c.uid for c in usable], rows, errors, provider.media_kind,
                          rest=pool[stopped:] if stopped else [], stopped_after=stopped)
        with ctx.lock:
            ctx.rankings[key] = ranking
        return ranking


def _rows(scored: list, scene, ctx: SelectionContext, first_position: int) -> list[Row]:
    return [Row(c, score, fit_bonus(c, scene.duration, ctx.orientation), preview, first_position + i,
                text_prob=text_prob) for i, (c, score, text_prob, preview) in enumerate(scored)]


def score_rest(provider, ranking: Ranking, scene, plan: SearchPlan, ctx: SelectionContext) -> int:
    """Score the candidates early exit skipped (pass 2 needs more choice). Returns how many were added."""
    if not ranking.rest:
        return 0
    rest, ranking.rest = ranking.rest, []
    new = _rows(_score(provider, rest, plan.queries, ctx), scene, ctx, len(ranking.rows))
    _multi_frame(provider, new, plan.queries, ctx)
    ranking.rows += new
    return len(rest)


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


def overlay_card(scene, meaning: SceneMeaning, warn: Callable[[str], None] | None = None) -> dict | None:
    """The number badge for a statistic line shown ON real footage: the same narration-only text as a number
    card, checked the same way. None if the card has no number (or fails the check - never drawn then)."""
    card = meaning.card or build_card(scene.text, meaning.keyword, meaning.names).to_dict()
    if not card.get("number"):
        return None
    headline, subtitle = card["headline"], card.get("subtitle", "")
    bad = check_card(headline, subtitle, scene.text)
    if bad:
        (warn or log.warning)(f"Scene {scene.index}: badge text '{headline} {subtitle}' has {', '.join(bad)}, "
                              "which the narration does not say; no badge drawn")
        return None
    return {"headline": headline, "subtitle": subtitle, "kind": card.get("kind"), "source": "narration",
            "numbers_shown": card.get("numbers_shown", []), "style": "badge (lower third, fades in)"}


def statistic_mode(meaning: SceneMeaning) -> str | None:
    """Step 0 of the rule: None (not a statistic line), "overlay" (footage + number badge) or "card"."""
    s = get_settings()
    if not (meaning.flags.get("statistic") and meaning.number and s.GENERATED_FOR_STATISTICS):
        return None
    if s.STATISTIC_OVERLAY:
        return "overlay"
    return "card" if provider_status("generated") is None else None


def _statistic_card_line(meaning: SceneMeaning) -> bool:
    """Does this line go straight to a number card (STATISTIC_OVERLAY=false)?"""
    return statistic_mode(meaning) == "card"


def _logo_notes(name: str, rows: list[Row]) -> list[str]:
    """ "pixabay_video: logo check rejected 2, best: ..." (best-scoring rejected ones first)."""
    rejected = sorted((r for r in rows if r.decision()[0] == "rejected"), key=lambda r: (-r.score, r.position))
    if not rejected:
        return []
    top = [f"{r.candidate.uid} (logo prob {r.logo:.2f}, scene {r.score:.3f})" for r in rejected[:3]]
    checked = sum(r.checked for r in rows)  # the check stops once the winner is certain (Ranking.ordered)
    return [f"{name}: logo check rejected {len(rejected)}, best: " + ", ".join(top) + f" ({checked} checked)"]


def _vlm_facts(ctx: SelectionContext, scene_index: int, uid: str) -> dict | None:
    said = ctx.vlm.get(scene_index) or {}
    scores = said.get("scores") or {}
    if not scores:
        return None
    return {"score": scores.get(uid), "judged": scores, "why": said.get("why", ""), "priority": said.get("priority"),
            "model": said.get("model")}


def _download_and_test(provider, cand: Candidate, plan: SearchPlan, ctx: SelectionContext) -> dict:
    """The slow, choice-free part of trying a clip: download, verify, frame logo check. Only facts, no
    decision, so it can run early (pass 1 prefetch) and give exactly what pass 2 would have found."""
    s = get_settings()
    folder = ctx.work / "clips"
    folder.mkdir(parents=True, exist_ok=True)
    try:
        path = provider.fetch(cand, folder)
    except (DownloadError, media.MediaError) as e:
        return {"error": str(e)}
    problem = verify_media(path, cand.kind)
    out = {"path": path, "problem": problem}
    if not problem and s.LOGO_CHECK:
        try:
            out["frames"] = frame_logo_check(cand, path, plan.queries, ctx.scorer, ctx.work / "frames")
        except Exception as e:  # frames could not be scored: the thumbnail check still stands
            log.warning("frame logo check of %s failed: %s", cand.uid, e)
            out["frame_error"] = str(e)
    return out


def _prefetch(provider, row: Row, plan: SearchPlan, ctx: SelectionContext) -> None:
    """Pass 1: download + test the scene's likely winner now, in parallel with the other scenes."""
    uid = row.candidate.uid
    with footage_cache.single_flight(("prefetch", id(ctx), uid)):
        with ctx.lock:
            if uid in ctx.prefetched:
                return
        facts = _download_and_test(provider, row.candidate, plan, ctx)
        with ctx.lock:
            ctx.prefetched[uid] = facts


def _fetch_and_check(name: str, provider, row: Row, scene, plan: SearchPlan, ctx: SelectionContext,
                     download_notes: list[str]) -> tuple[Path, dict | None, str] | None:
    """Download the row's clip, verify it, run the frame logo check. (path, logo report, note) or None.
    Uses the pass-1 prefetch when there is one (the same facts, already measured)."""
    s = get_settings()
    cand = row.candidate
    logo_facts = row.decision()[2]
    with ctx.lock:
        facts = ctx.prefetched.pop(cand.uid, None)
    if facts is None:
        facts = _download_and_test(provider, cand, plan, ctx)
    if "error" in facts:
        download_notes.append(f"{cand.uid} download failed ({facts['error']})")
        return None
    path, problem = facts["path"], facts["problem"]
    if problem:
        ctx.warn(f"Scene {scene.index}: downloaded clip {cand.uid} is corrupted ({problem}); "
                 "tried the next candidate")
        download_notes.append(f"{cand.uid} corrupted ({problem})")
        return None
    logo_report, frame_note = None, ""
    if s.LOGO_CHECK:
        if "frames" in facts:
            rejected, frame_facts = facts["frames"]
        else:
            rejected, frame_facts = False, []
            frame_note = f"; frame check not possible ({facts.get('frame_error', 'not run')})"
        if rejected:
            bad = next(f for f in frame_facts if f["result"] == "rejected")
            where = f"at {bad['time']:.1f} s" if bad["time"] is not None else "in the picture"
            ctx.used.add(cand.uid)  # never try it again in this video
            ctx.warn(f"Scene {scene.index}: clip {cand.uid} shows a logo {where} of the downloaded clip "
                     f"(logo prob {bad['logo_prob']:.2f} > {s.LOGO_REJECT_PROB:.2f}); "
                     "tried the next candidate")
            download_notes.append(f"{cand.uid} rejected by the frame logo check ({where}, "
                                  f"logo prob {bad['logo_prob']:.2f})")
            return None
        logo_report = {"thumbnail": logo_facts, "frames": frame_facts,
                       "result": (logo_facts or {}).get("result", "ok")}
        if frame_facts:
            frame_note = (f"; {len(frame_facts)} clip frame(s) passed the logo check "
                          f"(max logo prob {max(f['logo_prob'] for f in frame_facts):.2f})")
    return path, logo_report, frame_note


def _try_provider(name: str, provider, ranking: Ranking, scene, plan: SearchPlan, ctx: SelectionContext,
                  notes: list[str]) -> Selection | None:
    """Steps 2-5 for one source: the best passing candidate that downloads cleanly, or None (+ notes)."""
    s = get_settings()
    if ranking.errors:
        ctx.warn(f"Footage source {name} failed ({ranking.errors[0]}); used the next source")
        if not ranking.found:
            notes.append(f"{name} failed ({ranking.errors[0]})")
            return None
    if not [uid for uid in ranking.usable_uids if uid not in ctx.used]:
        notes.append(f"{name}: no usable results ({ranking.found} found, all excluded or already used)")
        return None
    rows = [r for r in ranking.rows if r.candidate.uid not in ctx.used]
    if not rows:
        notes.append(f"{name}: previews could not be scored")
        return None
    judged = (ctx.vlm.get(scene.index) or {}).get("scores") or {}
    vlm_rejected = [uid for uid, v in judged.items() if v <= s.VLM_REJECT_SCORE
                    and any(r.candidate.uid == uid for r in rows)]
    tries, download_notes = 0, []
    for row in ranking.ordered(ctx.scorer, s.LOGO_CHECK, vlm_scores=judged):
        cand, score, final = row.candidate, row.score, row.final
        if cand.uid in ctx.used:
            continue
        if tries >= MAX_DOWNLOAD_TRIES:
            break
        tries += 1
        got = _fetch_and_check(name, provider, row, scene, plan, ctx, download_notes)
        if got is None:
            continue
        path, logo_report, frame_note = got
        logo_facts = row.decision()[2]
        ctx.used.add(cand.uid)
        reason = (f"{name}: score {score:.3f} >= {s.STOCK_MIN_SCORE:.2f} for query '{cand.query}' "
                  f"(best of {len(rows)} candidates, order bonus {final - score:+.2f})")
        if row.frame_scores:
            reason += f"; score over {len(row.frame_scores)} frames (thumbnail {row.thumb_score:.3f})"
        if row.text_penalty:
            reason += f"; text-heavy picture (prob {row.text_prob:.2f}): order -{row.text_penalty:.2f}"
        if logo_facts:
            reason += f"; logo check {logo_facts['result']} (logo prob {logo_facts['logo_prob']:.2f})"
        reason += frame_note
        if cand.uid in judged:
            reason += f"; VLM judge {judged[cand.uid]}/5 (judged {len(judged)} candidates, higher rating first)"
        if vlm_rejected:
            reason += f"; VLM judge dropped {', '.join(vlm_rejected)} (<= {s.VLM_REJECT_SCORE}/5)"
        earlier = notes + _logo_notes(name, rows) + download_notes
        if earlier:
            reason += "; earlier: " + "; ".join(earlier)
        return Selection(scene.index, cand, path, score, round(final, 4), reason, cand.query, logo_report,
                         vlm=_vlm_facts(ctx, scene.index, cand.uid), details=row.details())
    notes += _logo_notes(name, rows)
    if tries == 0:  # nothing passed the threshold (after the logo check and the judge)
        kept = [r for r in rows if r.decision()[0] != "rejected"]
        passing = [r for r in kept if r.score >= s.STOCK_MIN_SCORE]
        if passing:  # they passed CLIP, but the independent judge dropped every one of them
            notes.append(f"{name}: {len(passing)} candidate(s) passed {s.STOCK_MIN_SCORE:.2f} (best "
                         f"{max(r.score for r in passing):.3f}) but the VLM judge dropped them "
                         f"(<= {s.VLM_REJECT_SCORE}/5: {', '.join(vlm_rejected)})")
        elif kept:
            notes.append(f"{name}: best score {max(r.score for r in kept):.3f} < {s.STOCK_MIN_SCORE:.2f}")
        return None
    notes += download_notes
    notes.append(f"{name}: no passing candidate could be downloaded")
    return None


def _best_available(ranking: Ranking, ctx: SelectionContext, judged: dict) -> Row | None:
    """The row the rule would try first for this source right now (None = nothing passes)."""
    s = get_settings()
    return next((r for r in ranking.ordered(ctx.scorer, s.LOGO_CHECK, vlm_scores=judged)
                 if r.candidate.uid not in ctx.used), None)


def _near_miss_rows(scene, ctx: SelectionContext, names: list[str]) -> list[tuple[str, Row]]:
    """Candidates of every searched source scoring in [adaptive floor, STOCK_MIN_SCORE), best first."""
    s = get_settings()
    floor = adaptive_floor()
    out = []
    for name in names:
        ranking = ctx.rankings.get((scene.index, name))
        for row in ranking.rows if ranking else []:
            if floor <= row.score < s.STOCK_MIN_SCORE and row.candidate.uid not in ctx.used:
                out.append((name, row))
    return sorted(out, key=lambda nr: (-nr[1].best_possible, nr[1].position))


def _adaptive(scene, plan: SearchPlan, ctx: SelectionContext, notes: list[str], names: list[str]) -> Selection | None:
    """Step 6: nothing passed STOCK_MIN_SCORE. A near-miss candidate is used only if the judge agrees."""
    s = get_settings()
    floor = adaptive_floor()
    band = _near_miss_rows(scene, ctx, names)
    if not band:
        return None
    if ctx.judge is None:
        notes.append(f"adaptive pass off: {len(band)} candidate(s) scored {floor:.3f}-{s.STOCK_MIN_SCORE:.2f} but "
                     "there is no independent judge (VLM_PROVIDER=none)")
        return None
    said = ctx.vlm.get(scene.index) or {}
    judged = said.get("scores") or {}
    if not judged:
        why = said.get("error") or said.get("skipped") or "not judged"
        notes.append(f"adaptive pass: {len(band)} candidate(s) scored {floor:.3f}-{s.STOCK_MIN_SCORE:.2f}, but the "
                     f"judge gave no rating ({why})")
        return None
    accepted = sorted(((n, r) for n, r in band if judged.get(r.candidate.uid, 0) >= s.VLM_ACCEPT_SCORE),
                      key=lambda nr: (-judged[nr[1].candidate.uid], -nr[1].final, nr[1].position))
    if not accepted:
        rated = ", ".join(f"{r.candidate.uid} {judged[r.candidate.uid]}/5" for _, r in band if r.candidate.uid in judged)
        notes.append(f"adaptive pass: the judge rated the near-miss candidates below {s.VLM_ACCEPT_SCORE}/5 "
                     f"({rated or 'none rated'})")
        return None
    download_notes: list[str] = []
    for name, row in accepted[:MAX_DOWNLOAD_TRIES]:
        if s.LOGO_CHECK and not row.checked:
            _check_logos([row], ctx.scorer)
        if row.decision()[0] == "rejected":
            download_notes.append(f"{row.candidate.uid} rejected by the logo check (logo prob {row.logo:.2f})")
            continue
        got = _fetch_and_check(name, get_provider(name), row, scene, plan, ctx, download_notes)
        if got is None:
            continue
        path, logo_report, frame_note = got
        cand, rating = row.candidate, judged[row.candidate.uid]
        ctx.used.add(cand.uid)
        reason = (f"adaptive threshold: no candidate reached {s.STOCK_MIN_SCORE:.2f}; {name} {cand.uid} scored "
                  f"{row.score:.3f} >= {floor:.3f} (STOCK_MIN_SCORE x (1 - RELATIVE_MARGIN {s.RELATIVE_MARGIN:g})) "
                  f"and the independent VLM judge rated it {rating}/5 >= {s.VLM_ACCEPT_SCORE} "
                  f"(query '{cand.query}'){frame_note}")
        earlier = notes + download_notes
        if earlier:
            reason += "; earlier: " + "; ".join(earlier)
        return Selection(scene.index, cand, path, row.score, row.final, reason, cand.query, logo_report,
                         vlm=_vlm_facts(ctx, scene.index, cand.uid),
                         details={**row.details(), "adaptive": True, "adaptive_floor": floor})
    notes += download_notes
    notes.append("adaptive pass: no judge-approved candidate could be downloaded")
    return None


def select_clip(scene, meaning: SceneMeaning, plan: SearchPlan, ctx: SelectionContext) -> Selection:
    """Apply the selection rule for one scene (pass 2). Never raises for provider trouble.

    Uses the scene's Rankings from pass 1 (or makes them now, e.g. for a clip swap).
    ctx.used holds every clip already chosen in this video - including one that failed a
    quality check - so it is never picked again.
    """
    s = get_settings()
    notes: list[str] = []
    mode = statistic_mode(meaning)

    if mode == "card":
        card = _card(scene, meaning, ctx.warn)
        if card.uid not in ctx.used:
            ctx.used.add(card.uid)
            return Selection(scene.index, card, None, None, None,
                             f"statistic line: number card '{card.extra['headline']}' taken from the narration "
                             "(GENERATED_FOR_STATISTICS=true, STATISTIC_OVERLAY=false)")

    active = []
    for name in s.provider_order:
        if name == "generated":
            continue  # the last resort, below
        status = provider_status(name)  # checked per scene: a provider can be switched off mid-job
        if status:
            notes.append(f"{name} skipped ({status})")
            continue
        active.append(name)

    judged = (ctx.vlm.get(scene.index) or {}).get("scores") or {}
    margin = s.PREFER_VIDEO_MARGIN
    chosen, tried = None, set()
    for i, name in enumerate(active):
        if name in tried:
            continue
        provider = get_provider(name)
        ranking = get_ranking(provider, scene, plan, ctx)
        preference = ""
        best = _best_available(ranking, ctx, judged) if provider.media_kind == "video" else None
        nxt = next((n for n in active[i + 1:] if n not in tried), None)
        if best is not None and best.score < s.STOCK_MIN_SCORE + margin and nxt \
                and get_provider(nxt).media_kind == "image":
            # A passing but not clearly good video: does the next photo source beat it by more than the margin?
            photo = get_provider(nxt)
            photo_ranking = get_ranking(photo, scene, plan, ctx)
            photo_best = _best_available(photo_ranking, ctx, judged)
            if photo_best is not None and photo_best.final > best.final + margin:
                tried.add(nxt)
                chosen = _try_provider(nxt, photo, photo_ranking, scene, plan, ctx, notes)
                if chosen:
                    chosen.reason += (f"; photo-motion preferred: order score {photo_best.final:.3f} > best video "
                                      f"{best.final:.3f} + PREFER_VIDEO_MARGIN {margin:.2f}")
                    break
            elif photo_best is not None:
                preference = (f"; video preferred over photo-motion ({nxt} best {photo_best.final:.3f} is not more "
                              f"than {margin:.2f} above {best.final:.3f})")
        elif best is not None and nxt and get_provider(nxt).media_kind == "image":
            preference = f"; video scored >= {s.STOCK_MIN_SCORE + margin:.2f}: photo sources not searched"
        tried.add(name)
        chosen = _try_provider(name, provider, ranking, scene, plan, ctx, notes)
        if chosen is None and ranking.rest:  # early exit skipped some: score them before giving up on this source
            notes.append(f"{name}: early exit had stopped after {ranking.stopped_after} candidates; "
                         f"scored the other {score_rest(provider, ranking, scene, plan, ctx)}")
            chosen = _try_provider(name, provider, ranking, scene, plan, ctx, notes)
        if chosen:
            chosen.reason += preference
            break

    if chosen is None:
        chosen = _adaptive(scene, plan, ctx, notes, active)

    if chosen is not None:
        if mode == "overlay":
            badge = overlay_card(scene, meaning, ctx.warn)
            if badge:
                chosen.overlay = badge
                chosen.reason = (f"statistic line on real footage: number badge '{badge['headline']}' from the "
                                 f"narration drawn over the clip (STATISTIC_OVERLAY=true); {chosen.reason}")
        return chosen

    card = _card(scene, meaning, ctx.warn)
    ctx.used.add(card.uid)
    if mode == "overlay":
        reason = (f"statistic line: no stock clip passed the rule ({'; '.join(notes) or 'no stock sources active'}) "
                  f"-> number card '{card.extra['headline']}' taken from the narration")
    else:
        reason = (f"no stock source passed the rule ({'; '.join(notes) or 'no stock sources active'}) "
                  f"-> generated motion card '{card.extra['headline']}'")
    return Selection(scene.index, card, None, None, None, reason)


def prepare_rankings(scenes: list, meanings: list[SceneMeaning], plans: list[SearchPlan],
                     ctx: SelectionContext) -> None:
    """Pass 1: search + preview + score footage for FOOTAGE_WORKERS scenes at the same time.

    For each scene it walks the sources in PROVIDER_ORDER until one has a candidate that passes the
    threshold and the logo check (that source will very likely be chosen); a passing video below
    STOCK_MIN_SCORE + PREFER_VIDEO_MARGIN also prepares the next photo source (pass 2 compares them).
    Nothing is CHOSEN here, so the no-repeat rule cannot be broken by two threads. A problem only means
    pass 2 does that work itself.
    """
    s = get_settings()

    def prepare(i: int) -> None:
        if _statistic_card_line(meanings[i]):
            return  # goes straight to a number card: no footage needed
        try:
            names = [n for n in s.provider_order if n != "generated" and not provider_status(n)]
            for k, name in enumerate(names):
                provider = get_provider(name)
                ranking = get_ranking(provider, scenes[i], plans[i], ctx)
                best = next(ranking.ordered(ctx.scorer, s.LOGO_CHECK), None)
                if best is not None:
                    nxt = names[k + 1] if k + 1 < len(names) else None
                    winner = (provider, best)
                    if (provider.media_kind == "video" and best.score < s.STOCK_MIN_SCORE + s.PREFER_VIDEO_MARGIN
                            and nxt and get_provider(nxt).media_kind == "image"):
                        photo = get_provider(nxt)
                        photo_best = next(get_ranking(photo, scenes[i], plans[i], ctx).ordered(ctx.scorer,
                                                                                              s.LOGO_CHECK), None)
                        if photo_best is not None and photo_best.final > best.final + s.PREFER_VIDEO_MARGIN:
                            winner = (photo, photo_best)
                    if s.PREFETCH_CLIPS:
                        _prefetch(winner[0], winner[1], plans[i], ctx)
                    return
        except Exception as e:  # pass 2 tries again and handles it the normal way
            log.warning("footage pass 1 for scene %d failed: %s", scenes[i].index, e)

    _pool_map(prepare, list(range(len(scenes))), s.FOOTAGE_WORKERS)


def judge_scenes(scenes: list, meanings: list[SceneMeaning], ctx: SelectionContext) -> dict:
    """Pass 1.5: the independent judge rates the top candidates of the most uncertain scenes.

    Priority (measurable): 0 = nothing passed STOCK_MIN_SCORE but near-misses exist (the adaptive pass needs a
    rating), 1 = the best passing score is below STOCK_MIN_SCORE + PREFER_VIDEO_MARGIN (a close call),
    2 = the rest; lower score first within a group. At most VLM_MAX_SCENES_PER_JOB scenes (0 = all).
    Results go to ctx.vlm[scene index]. Returns facts for the manifest."""
    s = get_settings()
    if ctx.judge is None:
        return {"judge": None}
    names = [n for n in s.provider_order if n != "generated" and not provider_status(n)]
    k = max(1, s.VLM_TOP_K)
    todo = []
    for scene, meaning in zip(scenes, meanings):
        if _statistic_card_line(meaning):
            continue
        passing = None
        for name in names:
            ranking = ctx.rankings.get((scene.index, name))
            rows = [r for r in (ranking.rows if ranking else []) if r.score >= s.STOCK_MIN_SCORE
                    and r.decision()[0] != "rejected"]
            if rows:
                passing = sorted(rows, key=lambda r: (-r.best_possible, r.position))[:k]
                break
        if passing:
            top = passing[0].score
            group = 1 if top < s.STOCK_MIN_SCORE + s.PREFER_VIDEO_MARGIN else 2
            if group == 2 and not s.VLM_JUDGE_CONFIDENT:
                ctx.vlm[scene.index] = {"skipped": "confident: best score >= STOCK_MIN_SCORE + PREFER_VIDEO_MARGIN "
                                                   "(VLM_JUDGE_CONFIDENT=false)", "priority": "confident"}
                continue
            todo.append(((group, top), scene, ["close call", "confident"][group - 1], passing))
            continue
        band = [r for _, r in _near_miss_rows(scene, ctx, names)][:k]
        if band:
            todo.append(((0, -band[0].score), scene, "near miss (adaptive pass)", band))
    todo.sort(key=lambda t: (t[0], t[1].index))
    limit = s.VLM_MAX_SCENES_PER_JOB or len(todo)
    model = ctx.judge.info().get("model")
    judged, started = 0, time.perf_counter()
    for n, (_, scene, label, rows) in enumerate(todo):
        if n >= limit:
            ctx.vlm[scene.index] = {"skipped": f"VLM_MAX_SCENES_PER_JOB={s.VLM_MAX_SCENES_PER_JOB} reached",
                                    "priority": label}
            continue
        try:
            scores, why = ctx.judge.judge(scene.text, getattr(scene, "search_text", None),
                                          [(r.candidate.uid, r.candidate, r.preview) for r in rows])
            ctx.vlm[scene.index] = {"scores": scores, "why": why, "priority": label, "model": model}
            judged += 1
        except JudgeUnavailable as e:
            ctx.warn(f"The VLM judge is unavailable ({e}); the remaining scenes were not judged")
            for _, later, later_label, _ in todo[n:]:
                ctx.vlm[later.index] = {"error": str(e), "priority": later_label}
            break
        except JudgeError as e:
            log.warning("VLM judge failed for scene %d: %s", scene.index, e)
            ctx.vlm[scene.index] = {"error": str(e), "priority": label}
    return {"judge": ctx.judge.info(), "scenes_wanting_a_rating": len(todo), "scenes_judged": judged,
            "seconds": round(time.perf_counter() - started, 3)}


def select_clips(scenes: list, meanings: list[SceneMeaning], plans: list[SearchPlan], ctx: SelectionContext,
                 on_scene_done: Callable[[int], None] | None = None) -> tuple[list[Selection], dict]:
    """One clip per scene, in passes:

    pass 1 (parallel): every scene searches, downloads previews and scores them - the slow part;
    pass 1.5 (with a judge): the most uncertain scenes get an independent rating;
    pass 2 (one scene at a time, in scene order): the selection rule picks, downloads and checks the
    clip, honouring the no-repeat rule exactly as before.
    Same inputs -> same choices, whatever FOOTAGE_WORKERS is. Returns (selections, timing facts).
    """
    footage_cache.prune()
    t0 = time.perf_counter()
    prepare_rankings(scenes, meanings, plans, ctx)
    t1 = time.perf_counter()
    vlm_facts = judge_scenes(scenes, meanings, ctx)
    t2 = time.perf_counter()
    selections = []
    for i, (scene, meaning, plan) in enumerate(zip(scenes, meanings, plans)):
        selections.append(select_clip(scene, meaning, plan, ctx))
        if on_scene_done:
            on_scene_done(i)
    s = get_settings()
    facts = {"quality_mode": s.QUALITY_MODE, "footage_workers": max(1, s.FOOTAGE_WORKERS),
             "early_exit_rankings": sum(1 for r in ctx.rankings.values() if r.stopped_after),
             "pass1_search_score_seconds": round(t1 - t0, 3),
             "pass1_5_vlm_seconds": round(t2 - t1, 3),
             "pass2_select_download_seconds": round(time.perf_counter() - t2, 3),
             "candidates_scored": sum(len(r.rows) for r in ctx.rankings.values()),
             "vlm": vlm_facts}
    return selections, facts
