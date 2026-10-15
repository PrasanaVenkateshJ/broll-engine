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
4. If the best CLIP score >= STOCK_MIN_SCORE: download the best candidate, check it with
   ffprobe; if it is corrupted, take the next passing one. Done.
   Otherwise (or if the source failed) go to the next source.
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

    def info(self) -> dict:
        return {"scorer": self.name}


class MockScorer(Scorer):
    """No model: share of a query's words found in the clip's tags (0.15 .. 0.30)."""
    name = "mock"
    needs_images = False

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
    _lock = threading.Lock()
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

    def score(self, candidates, previews, texts):
        from PIL import Image

        model, preprocess, tokenizer, torch = self._load()
        with self._lock, torch.no_grad():
            text = model.encode_text(tokenizer([f"a photo of {t}" for t in texts]))
            text = text / text.norm(dim=-1, keepdim=True)
            images = torch.stack([preprocess(Image.open(p).convert("RGB")) for p in previews])
            image = model.encode_image(images)
            image = image / image.norm(dim=-1, keepdim=True)
            # Average cosine similarity of each picture to the queries = "how well it shows the meaning".
            sims = (image @ text.T).mean(dim=1)
        return [round(float(x), 4) for x in sims]


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

    def to_dict(self) -> dict:
        c = self.candidate
        return {"provider": c.provider, "clip_id": c.id, "uid": c.uid, "kind": c.kind, "query": self.query or c.query,
                "title": c.title, "page_url": c.page_url, "source_url": c.download_url,
                "licence": c.licence_name, "licence_url": c.licence_url, "attribution": c.attribution,
                "commercial_use": c.commercial_use, "watermark": c.watermark, "ai_generated": c.ai_generated,
                "generated": c.generated, "reason": self.reason, "relevance_score": self.clip_score,
                "final_score": self.final_score, "width": c.width, "height": c.height, "duration": c.duration}


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
    """[(candidate, score)] for the candidates whose preview could be loaded."""
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
    try:
        scores = ctx.scorer.score(candidates, previews, texts)
    except Exception as e:  # a broken preview image must not fail the job
        log.warning("scoring failed: %s", e)
        return []
    return list(zip(candidates, scores))


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
        ranked = sorted(((c, score, score + fit_bonus(c, seconds, ctx.orientation)) for c, score in scored),
                        key=lambda x: x[2], reverse=True)
        best = max(score for _, score, _ in ranked)
        passing = [r for r in ranked if r[1] >= s.STOCK_MIN_SCORE]
        if not passing:
            notes.append(f"{name}: best score {best:.3f} < {s.STOCK_MIN_SCORE:.2f}")
            continue
        for cand, score, final in passing[:MAX_DOWNLOAD_TRIES]:
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
            ctx.used.add(cand.uid)
            reason = (f"{name}: score {score:.3f} >= {s.STOCK_MIN_SCORE:.2f} for query '{cand.query}' "
                      f"(best of {len(ranked)} candidates, fit bonus +{final - score:.2f})")
            if notes:
                reason += "; earlier: " + "; ".join(notes)
            return Selection(scene.index, cand, path, score, round(final, 4), reason, cand.query)
        notes.append(f"{name}: no passing candidate could be downloaded")

    card = _card(scene.index, meaning)
    ctx.used.add(card.uid)
    return Selection(scene.index, card, None, None, None,
                     f"no stock source passed the rule ({'; '.join(notes) or 'no stock sources active'}) "
                     f"-> generated motion card '{card.extra['headline']}'")
