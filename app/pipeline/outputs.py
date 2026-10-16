"""Files we hand back: captions.srt (here now), timeline.json and manifest.json.

Captions are built from the word timestamps:
- a caption ("cue") holds at most CAPTION_MAX_LINES lines of CAPTION_MAX_CHARS_PER_LINE,
- and lasts at most CAPTION_MAX_SECONDS,
- a new cue starts after every sentence end,
- a cue never crosses a scene boundary (captions are burned into each scene's segment).
"""
import json
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings
from app.pipeline.scenes import ABBREVIATIONS, SENTENCE_END, Scene


@dataclass
class Cue:
    start: float
    end: float
    lines: list[str]
    scene_index: int

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def wrap(tokens: list[str], max_chars: int) -> list[str]:
    """Put words into as few lines as possible, each <= max_chars (a longer single word gets its own line)."""
    lines: list[str] = []
    for token in tokens:
        if lines and len(lines[-1]) + 1 + len(token) <= max_chars:
            lines[-1] += " " + token
        else:
            lines.append(token)
    return lines


def balanced(tokens: list[str], max_chars: int) -> list[str]:
    """Like wrap(), but if it needs 2 lines, make the 2 lines about the same length (easier to read)."""
    lines = wrap(tokens, max_chars)
    if len(lines) != 2:
        return lines
    best = None
    for k in range(1, len(tokens)):
        top, bottom = " ".join(tokens[:k]), " ".join(tokens[k:])
        if len(top) <= max_chars and len(bottom) <= max_chars:
            if best is None or max(len(top), len(bottom)) < max(len(best[0]), len(best[1])):
                best = (top, bottom)
    return list(best) if best else lines


def build_cues(scenes: list[Scene]) -> list[Cue]:
    s = get_settings()
    max_chars, max_lines = s.CAPTION_MAX_CHARS_PER_LINE, s.CAPTION_MAX_LINES
    cues: list[Cue] = []

    for scene in scenes:
        groups, current = [], []
        for word in scene.words:
            candidate = current + [word]
            too_wide = len(wrap([w.text for w in candidate], max_chars)) > max_lines
            too_long = candidate[-1].end - candidate[0].start > s.CAPTION_MAX_SECONDS
            if current and (too_wide or too_long):
                groups.append(current)
                candidate = [word]
            current = candidate
            if SENTENCE_END.search(word.text) and word.text.lower() not in ABBREVIATIONS:
                groups.append(current)
                current = []
        if current:
            groups.append(current)

        scene_cues = [Cue(start=max(g[0].start, scene.start), end=g[-1].end,
                          lines=balanced([w.text for w in g], max_chars), scene_index=scene.index) for g in groups]
        # Stay on screen a little after the last word, but never past the next cue or the scene end.
        for i, cue in enumerate(scene_cues):
            limit = scene_cues[i + 1].start if i + 1 < len(scene_cues) else scene.end
            cue.end = round(min(max(cue.end + s.CAPTION_LINGER_SECONDS, cue.start + 0.3), limit, scene.end), 3)
            cue.start = round(min(cue.start, cue.end), 3)
        cues.extend(c for c in scene_cues if c.end > c.start)
    return cues


def srt_time(seconds: float) -> str:
    """12.5 -> "00:00:12,500" (SRT uses a comma before the milliseconds)."""
    ms = int(round(max(seconds, 0.0) * 1000))
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    secs, ms = divmod(ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def to_srt(cues: list[Cue]) -> str:
    blocks = [f"{i}\n{srt_time(c.start)} --> {srt_time(c.end)}\n{c.text}\n" for i, c in enumerate(cues, start=1)]
    return "\n".join(blocks)


def write_srt(cues: list[Cue], path: Path) -> Path:
    path.write_text(to_srt(cues), encoding="utf-8")
    return path


def write_json(data: dict, path: Path) -> Path:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


# ---------- timeline.json + manifest.json ----------

def build_timeline(job_id: str, input_type: str, duration: float, language: str, scenes: list[Scene],
                   meanings: list, plans: list, selections: list, segments: dict, videos: list[dict]) -> dict:
    """One entry per scene: what was said, when, what we searched for, what we used, and why."""
    out = []
    for scene, meaning, plan, selection in zip(scenes, meanings, plans, selections):
        clip = selection.to_dict()
        out.append({
            "index": scene.index, "text": scene.text, "start": scene.start, "end": scene.end,
            "duration": scene.duration, "visual_hint": scene.visual_hint,
            "query": clip["query"] or (plan.queries[0] if plan.queries else ""), "queries": plan.queries,
            "flags": meaning.flags,
            "provider": clip["provider"], "clip_id": clip["clip_id"], "page_url": clip["page_url"],
            "licence": clip["licence"], "licence_url": clip["licence_url"], "attribution": clip["attribution"],
            "commercial_use": clip["commercial_use"], "watermark": clip["watermark"],
            "ai_generated": clip["ai_generated"], "generated": clip["generated"], "reason": clip["reason"],
            "relevance_score": clip["relevance_score"],
            # Thumbnail + downloaded-clip-frame logo test (None for generated cards / LOGO_CHECK=false).
            "logo_check": clip["logo_check"],
            "clip_details": {k: clip[k] for k in ("uid", "kind", "title", "source_url", "final_score", "width",
                                                   "height", "duration")},
            # The full answer + its fingerprint, so a re-render / clip swap reuses it instead of asking again.
            "understanding": meaning.to_dict(),
            "safety": plan.to_dict(),
            "segments": segments.get(scene.index, {}),
            "words": [{"text": w.text, "start": w.start, "end": w.end} for w in scene.words],
        })
    return {"job_id": job_id, "input_type": input_type, "duration": round(duration, 3), "language": language,
            "scenes": out, "videos": videos}


def rights_summary(selections: list) -> dict:
    """Licence / rights facts for the whole video."""
    licences: dict[str, int] = {}
    for sel in selections:
        licences[sel.candidate.licence_name] = licences.get(sel.candidate.licence_name, 0) + 1
    return {
        "licences": licences,
        "all_commercial_use": all(sel.candidate.commercial_use for sel in selections),
        "any_watermark": any(sel.candidate.watermark for sel in selections),
        "attribution_required": [
            {"scene": sel.scene_index, "attribution": sel.candidate.attribution,
             "licence": sel.candidate.licence_name, "licence_url": sel.candidate.licence_url}
            for sel in selections if sel.candidate.licence_name.startswith("CC BY")],
        "credits": [{"scene": sel.scene_index, "provider": sel.candidate.provider,
                     "attribution": sel.candidate.attribution, "page_url": sel.candidate.page_url}
                    for sel in selections if sel.candidate.attribution],
    }


def clip_type(selection) -> str:
    """"video" (a real video clip), "photo_motion" (still photo + Ken Burns) or "generated_card"."""
    c = selection.candidate
    if c.generated:
        return "generated_card"
    return "photo_motion" if c.kind == "image" else "video"


def footage_mix(selections: list) -> dict:
    """How many scenes use real stock video vs photo-motion vs generated cards (the goal: mostly video).

    Statistic lines get a number card ON PURPOSE (GENERATED_FOR_STATISTICS), so the shares are also
    given without them: that is the fair measure of how often a good video clip was found.
    """
    types = ("video", "photo_motion", "generated_card")
    counts = {t: 0 for t in types}
    for sel in selections:
        counts[clip_type(sel)] += 1
    stat_cards = sum(1 for sel in selections if sel.candidate.generated and sel.reason.startswith("statistic line"))
    others = {**counts, "generated_card": counts["generated_card"] - stat_cards}
    n, n_others = len(selections), len(selections) - stat_cards
    return {
        "scenes": n,
        "counts": counts,
        "share": {t: round(counts[t] / n, 3) if n else 0.0 for t in types},
        "statistic_cards": stat_cards,
        "share_without_statistic_cards": {t: round(others[t] / n_others, 3) if n_others else 0.0 for t in types},
    }


MANIFEST_SETTINGS = (
    "SEED", "MIN_SCENE_SECONDS", "MAX_SCENE_SECONDS", "PAUSE_SECONDS", "UNDERSTAND_PROVIDER", "LLM_MODEL",
    "PROVIDER_ORDER", "PROVIDERS_DISABLED", "RANK_SCORER", "CLIP_MODEL", "CLIP_PRETRAINED", "STOCK_MIN_SCORE",
    "DURATION_BONUS", "ORIENTATION_BONUS", "GENERATED_FOR_STATISTICS", "MAX_SLOWDOWN", "KEN_BURNS_ZOOM",
    "TARGET_LUMA", "TARGET_SATURATION", "QA_BLACK_SECONDS", "QA_FREEZE_SECONDS", "QA_MAX_DRIFT_SECONDS",
    "QA_RETRIES", "RENDER_FPS", "VIDEO_PRESET", "VIDEO_CRF", "AUDIO_SAMPLE_RATE", "AUDIO_BITRATE",
    "CAPTION_MAX_CHARS_PER_LINE", "CAPTION_MAX_LINES", "LOGO_CHECK", "LOGO_CROP_GRID", "LOGO_REJECT_PROB",
    "LOGO_PENALTY_PROB", "LOGO_PENALTY", "LOGO_FRAME_COUNT")

COST_METHOD = ("0 USD: every service used here is free - Groq free tier for the LLM (tokens counted in "
               "llm_tokens), Pixabay and Wikimedia Commons APIs are free, and Kokoro, Whisper, open_clip and "
               "FFmpeg run locally on the CPU. On paid plans: cost = llm_tokens x the LLM price per token + "
               "compute time (timings_seconds) x the server price.")


def build_manifest(*, job_id: str, created_at: str, app: dict, input_facts: dict, models: dict, providers: dict,
                   speech_check: dict, timings: dict, warnings: list[str], selections: list, videos: list[dict],
                   qa: dict) -> dict:
    """Everything needed to understand (and repeat) how this job was made."""
    s = get_settings()
    llm_tokens = int((models.get("understand") or {}).get("tokens_used") or 0)
    return {
        "job_id": job_id,
        "created_at": created_at,
        "app": app,
        "input": input_facts,
        "models": models,
        "providers": providers,
        "settings": {k: getattr(s, k) for k in MANIFEST_SETTINGS},
        "seed": s.SEED,
        "speech_check": speech_check,
        "timings_seconds": timings,
        "timings_note": "seconds per stage, measured up to the moment this file was written (upload not included)",
        "cost_estimate": {"total_usd": 0.0, "llm_tokens": llm_tokens, "method": COST_METHOD},
        "warnings": warnings,
        "rights": rights_summary(selections),
        "footage_mix": footage_mix(selections),
        "ai_generated": {
            "narration": "tts" in models,
            "visuals": any(sel.candidate.ai_generated for sel in selections),
            "generated_cards": sum(sel.candidate.generated for sel in selections),
            "note": "generated cards are drawn by FFmpeg (gradient + text), not by an AI model",
        },
        "qa": qa,
        "outputs": videos,
    }
