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
