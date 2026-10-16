"""Pick ONE clip per scene, with a rule anyone can check.

THE SELECTION RULE (its result and reason are written to timeline.json):
0. A statistic line (a number/percentage) gets an animated number card from `generated`
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
"""
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from app.config import get_settings
from app.pipeline import media
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
    """open_clip (MIT): how well does the picture match the text? Cosine similarity, about 0.1-0.4."""
    name = "clip"
    _lock = threading.RLock()  # re-entrant: score() holds it while helpers call _load()
    _model = None

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
        return cls._model

    def info(self):
        from importlib.metadata import version

        s = get_settings()
        return {"scorer": self.name, "library": "open_clip_torch", "version": version("open_clip_torch"),
                "model": s.CLIP_MODEL, "pretrained": s.CLIP_PRETRAINED}

    def __init__(self):
        self._embeddings: dict[str, object] = {}  # preview path -> picture embedding (scored twice: scene + logo)

    def _encode_texts(self, texts: list[str]):
        model, _, tokenizer, _ = self._load()
        text = model.encode_text(tokenizer(texts))
        return text / text.norm(dim=-1, keepdim=True)

    def _encode_pictures(self, pictures):
        """PIL pictures -> normalised CLIP embeddings."""
        model, preprocess, _, torch = self._load()
        image = model.encode_image(torch.stack([preprocess(p) for p in pictures]))
        return image / image.norm(dim=-1, keepdim=True)

    def _preview_embeddings(self, previews: list[Path]):
        from PIL import Image

        torch = self._load()[3]
        missing = [p for p in previews if str(p) not in self._embeddings]
        if missing:
            for p, emb in zip(missing, self._encode_pictures([Image.open(p).convert("RGB") for p in missing])):
                self._embeddings[str(p)] = emb
        return torch.stack([self._embeddings[str(p)] for p in previews])

    def score(self, candidates, previews, texts):
        torch = self._load()[3]
        with self._lock, torch.no_grad():
            text = self._encode_texts([f"a photo of {t}" for t in texts])
            # Average cosine similarity of each picture to the queries = "how well it shows the meaning".
            sims = (self._preview_embeddings(previews) @ text.T).mean(dim=1)
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
        out = []
        with self._lock, torch.no_grad():
            text = self._encode_texts(logo + neutral)
            for path in previews:
                views = logo_views(Image.open(path).convert("RGB"), s.LOGO_CROP_GRID)
                logits = model.logit_scale.exp() * (self._encode_pictures(views) @ text.T)
                out.append(round(float(logits.softmax(dim=-1)[:, :len(logo)].sum(dim=1).max()), 4))
        return out

    def score_images(self, images, texts):
        from PIL import Image

        torch = self._load()[3]
        with self._lock, torch.no_grad():
            text = self._encode_texts([f"a photo of {t}" for t in texts])
            sims = (self._encode_pictures([Image.fromarray(a) for a in images]) @ text.T).mean(dim=1)
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
    """What all scenes of one job share."""
    work: Path
    orientation: str                           # landscape | portrait | square (first aspect ratio)
    warn: Callable[[str], None]
    scorer: Scorer
    used: set[str] = field(default_factory=set)            # clip uids already in this video
    cache: dict = field(default_factory=dict)              # (provider, query, orientation) -> results


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


def _search(provider, queries: list[str], ctx: SelectionContext, scene_seconds: float):
    """All candidates for all queries (no duplicates), plus the error messages."""
    found: dict[str, Candidate] = {}
    errors: list[str] = []
    for query in queries:
        key = (provider.name, query, ctx.orientation)
        if key not in ctx.cache:
            try:
                ctx.cache[key] = provider.search(query, ctx.orientation, scene_seconds)
            except ProviderError as e:
                ctx.cache[key] = e
        result = ctx.cache[key]
        if isinstance(result, ProviderError):
            errors.append(str(result))
            continue
        for c in result:
            found.setdefault(c.uid, c)
    return list(found.values()), errors


def _score(provider, candidates: list[Candidate], texts: list[str], ctx: SelectionContext):
    """[(candidate, score, logo probability or None)] for the candidates whose preview could be loaded."""
    previews: list[Path | None] = [None] * len(candidates)
    if ctx.scorer.needs_images:
        folder = ctx.work / "previews"
        folder.mkdir(parents=True, exist_ok=True)

        def load(c):
            try:
                return provider.fetch_preview(c, folder)
            except (DownloadError, media.MediaError) as e:
                log.info("preview of %s failed: %s", c.uid, e)
                return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            previews = list(pool.map(load, candidates))
        pairs = [(c, p) for c, p in zip(candidates, previews) if p is not None]
        candidates, previews = [c for c, _ in pairs], [p for _, p in pairs]
    if not candidates:
        return []
    s = get_settings()
    try:
        scores = ctx.scorer.score(candidates, previews, texts)
        logos: list[float | None] = [None] * len(scores)
        if s.LOGO_CHECK:  # only candidates that pass STOCK_MIN_SCORE can be chosen: check just those
            idx = [i for i, score in enumerate(scores) if score >= s.STOCK_MIN_SCORE]
            if idx:
                probs = ctx.scorer.logo_probs([candidates[i] for i in idx], [previews[i] for i in idx])
                for i, p in zip(idx, probs):
                    logos[i] = p
    except Exception as e:  # a broken preview image must not fail the job
        log.warning("scoring failed: %s", e)
        return []
    return list(zip(candidates, scores, logos))


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


def _card(scene_index: int, meaning: SceneMeaning) -> Candidate:
    if meaning.number:
        return GeneratedProvider().card(scene_index, meaning.number, meaning.keyword)
    return GeneratedProvider().card(scene_index, (meaning.keyword or "").upper())


def select_clip(scene, meaning: SceneMeaning, plan: SearchPlan, ctx: SelectionContext) -> Selection:
    """Apply the selection rule for one scene. Never raises for provider trouble.

    ctx.used holds every clip already chosen in this video - including one that failed a
    quality check - so it is never picked again.
    """
    s = get_settings()
    seconds = scene.duration
    notes: list[str] = []

    if meaning.flags.get("statistic") and meaning.number and s.GENERATED_FOR_STATISTICS \
            and provider_status("generated") is None:
        card = _card(scene.index, meaning)
        if card.uid not in ctx.used:
            ctx.used.add(card.uid)
            return Selection(scene.index, card, None, None, None,
                             f"statistic line: animated number card '{meaning.number}' (GENERATED_FOR_STATISTICS=true)")

    for name in s.provider_order:
        if name == "generated":
            continue  # the last resort, below
        status = provider_status(name)  # checked per scene: a provider can be switched off mid-job
        if status:
            notes.append(f"{name} skipped ({status})")
            continue
        provider = get_provider(name)
        candidates, errors = _search(provider, plan.queries, ctx, seconds)
        if errors:
            ctx.warn(f"Footage source {name} failed ({errors[0]}); used the next source")
            if not candidates:
                notes.append(f"{name} failed ({errors[0]})")
                continue
        usable = [c for c in candidates
                  if c.uid not in ctx.used and not find_excluded(c.search_text(), plan.exclude_terms)]
        if not usable:
            notes.append(f"{name}: no usable results ({len(candidates)} found, all excluded or already used)")
            continue
        scored = _score(provider, usable, plan.queries, ctx)
        if not scored:
            notes.append(f"{name}: previews could not be scored")
            continue
        rows, logo_rejected = [], []
        for c, score, logo in scored:
            result, penalty, facts = logo_decision(logo)
            if result == "rejected":
                logo_rejected.append((score, f"{c.uid} (logo prob {logo:.2f}, scene {score:.3f})"))
                continue
            rows.append((c, score, round(score + fit_bonus(c, seconds, ctx.orientation) - penalty, 4), facts))
        if logo_rejected:  # best-scoring rejected ones first: those are the ones that mattered
            top = [text for _, text in sorted(logo_rejected, reverse=True)[:3]]
            notes.append(f"{name}: logo check rejected {len(logo_rejected)}, best: " + ", ".join(top))
        if not rows:
            continue
        ranked = sorted(rows, key=lambda x: x[2], reverse=True)
        best = max(r[1] for r in ranked)
        passing = [r for r in ranked if r[1] >= s.STOCK_MIN_SCORE]
        if not passing:
            notes.append(f"{name}: best score {best:.3f} < {s.STOCK_MIN_SCORE:.2f}")
            continue
        for cand, score, final, logo_facts in passing[:MAX_DOWNLOAD_TRIES]:
            folder = ctx.work / "clips"
            folder.mkdir(parents=True, exist_ok=True)
            try:
                path = provider.fetch(cand, folder)
            except (DownloadError, media.MediaError) as e:
                notes.append(f"{cand.uid} download failed ({e})")
                continue
            problem = verify_media(path, cand.kind)
            if problem:
                ctx.warn(f"Scene {scene.index}: downloaded clip {cand.uid} is corrupted ({problem}); "
                         "tried the next candidate")
                notes.append(f"{cand.uid} corrupted ({problem})")
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
                    notes.append(f"{cand.uid} rejected by the frame logo check ({where}, "
                                 f"logo prob {bad['logo_prob']:.2f})")
                    continue
                logo_report = {"thumbnail": logo_facts, "frames": frame_facts,
                               "result": (logo_facts or {}).get("result", "ok")}
                if frame_facts:
                    frame_note = (f"; {len(frame_facts)} clip frame(s) passed the logo check "
                                  f"(max logo prob {max(f['logo_prob'] for f in frame_facts):.2f})")
            ctx.used.add(cand.uid)
            reason = (f"{name}: score {score:.3f} >= {s.STOCK_MIN_SCORE:.2f} for query '{cand.query}' "
                      f"(best of {len(ranked)} candidates, order bonus {final - score:+.2f})")
            if logo_facts:
                reason += f"; logo check {logo_facts['result']} (logo prob {logo_facts['logo_prob']:.2f})"
            reason += frame_note
            if notes:
                reason += "; earlier: " + "; ".join(notes)
            return Selection(scene.index, cand, path, score, round(final, 4), reason, cand.query, logo_report)
        notes.append(f"{name}: no passing candidate could be downloaded")

    card = _card(scene.index, meaning)
    ctx.used.add(card.uid)
    return Selection(scene.index, card, None, None, None,
                     f"no stock source passed the rule ({'; '.join(notes) or 'no stock sources active'}) "
                     f"-> generated motion card '{card.extra['headline']}'")
