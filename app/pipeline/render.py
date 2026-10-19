"""Turn the chosen clips into the final videos (one per aspect ratio).

For every scene and every aspect ratio we render ONE segment (a short mp4):
1. Fit the time: the segment lasts exactly the scene (counted in whole frames).
   - clip long enough  -> take a piece from it (skip up to 1 s of the start, often a fade),
   - a bit too short   -> slow it down, at most MAX_SLOWDOWN (25 %),
   - much too short    -> slow down 25 % and play it forward-then-backward in a loop
                          ("ping-pong": no jump, and never a frozen frame).
2. Fit the frame: scale + crop to 1920x1080 / 1080x1920 / 1080x1080.
   Subject-aware crop (when the scorer can look at pictures, i.e. RANK_SCORER=clip), see _clip_crop():
   CROP_STRIPS narrow strips across each of CROP_SAMPLE_FRAMES colour frames are scored with CLIP against the
   scene meaning; the strongest run of strips is the subject. The window is placed to hold the WHOLE subject
   (anywhere, not only left / centre / right) and pans smoothly if the subject moves. No clear subject -> centre
   crop + a warning. If the window still scores CROP_BLUR_MARGIN below the WHOLE frame, it loses the subject
   -> we show the whole clip on a blurred copy of itself ("blurred fit").
   Without CLIP (mock scorer, tests) the edge + motion energy decides (choose_window), and a subject wider
   than the crop (e.g. two people at both edges) also gives the blurred fit.
   The choice and its scores are recorded per segment ("crop" in timeline.json).
3. Look: measure mean brightness (YAVG) + colour (SATAVG) with signalstats, then pull them
   toward TARGET_LUMA / TARGET_SATURATION with the eq filter, so clips match each other.
4. Statistic line on real footage: draw its number badge (text from the narration only, cards.py).
5. Burn that scene's captions. 30 fps, yuv420p, H.264.
Still images get a slow Ken Burns zoom/pan; `generated` cards are drawn here (gradient + text).

Segments are cached by a key (clip + scene times + ratio + settings), so a later "swap
this clip" only re-renders one segment. Finally the segments are joined with stream copy
(no re-encoding) and the narration is added as AAC 48 kHz.
"""
import hashlib
import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from app.config import get_settings
from app.errors import AppError, ErrorCode
from app.pipeline import media
from app.pipeline.outputs import Cue
from app.pipeline.rank import Selection
from app.pipeline.scenes import Scene

log = logging.getLogger("app.pipeline.render")

RESOLUTIONS = {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080)}
ANALYSIS_WIDTH = 192  # sample frames are shrunk to this width (enough to find the subject)
CROP_ANALYSIS_WIDTH = 640  # colour frames for the CLIP crop check (a 9:16 window is then ~200 px wide)


def ratio_slug(ratio: str) -> str:
    """"16:9" -> "16x9" (safe in file names)."""
    return ratio.replace(":", "x")


def video_filename(ratio: str) -> str:
    return f"video_{ratio_slug(ratio)}.mp4"


def orientation_of(ratio: str) -> str:
    w, h = RESOLUTIONS[ratio]
    return "landscape" if w > h else "portrait" if h > w else "square"


# ---------- Burned captions (ASS subtitle format, drawn by FFmpeg's libass) ----------

# Fonts we ship (app/fonts, SIL OFL): the same files on Windows, Docker and Modal, so Tamil and Hindi
# captions never depend on which system fonts happen to be installed (Docker has none for them).
FONTS_DIR = Path(__file__).resolve().parent.parent / "fonts"
# Unicode block of each script we have a font for -> the language code used in CAPTION_FONTS_BY_LANGUAGE.
SCRIPT_LANGUAGES = (("ta", 0x0B80, 0x0BFF), ("hi", 0x0900, 0x097F))


def script_language(text: str) -> str | None:
    """"ta" if the text holds Tamil letters, "hi" for Devanagari, else None."""
    for lang, low, high in SCRIPT_LANGUAGES:
        if any(low <= ord(ch) <= high for ch in text):
            return lang
    return None


def caption_font(language: str | None, text: str = "") -> str:
    """Font for a caption: by the caption language first (Whisper code, e.g. "ta"), then by the script
    of the text itself (a safety net if the language code is missing), else CAPTION_FONT."""
    s = get_settings()
    fonts = s.caption_fonts_by_language
    lang = (language or "").strip().lower()
    if lang in fonts:
        return fonts[lang]
    return fonts.get(script_language(text) or "", s.CAPTION_FONT)


def font_scale(font: str) -> float:
    """Size factor for this font (CAPTION_FONT_SCALES), 1.0 if none is set."""
    return get_settings().caption_font_scales.get(font, 1.0)


def fonts_dir() -> Path:
    s = get_settings()
    return Path(s.CAPTION_FONTS_DIR) if s.CAPTION_FONTS_DIR else FONTS_DIR


def subtitles_filter(ass_name: str, folder: Path) -> str:
    """The FFmpeg filter that burns `ass_name` (a file in `folder`) with our fonts.
    The fonts are copied next to it once, so the filter only holds bare relative names
    (a Windows path such as C:/work/... breaks FFmpeg's filter syntax)."""
    source = fonts_dir()
    if source.is_dir():
        if not (folder / "fonts").is_dir():
            shutil.copytree(source, folder / "fonts")
        return f"subtitles=filename={ass_name}:fontsdir=fonts"
    return f"subtitles=filename={ass_name}"


def _ass_time(seconds: float) -> str:
    """ASS times look like 0:00:12.34 (hours:minutes:seconds.hundredths)."""
    cs = int(round(max(seconds, 0.0) * 100))
    hours, cs = divmod(cs, 360000)
    minutes, cs = divmod(cs, 6000)
    secs, cs = divmod(cs, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def _ass_text(lines: list[str]) -> str:
    # { } and \ are commands in ASS: replace them so captions are always shown as plain text.
    clean = [line.replace("\\", "/").replace("{", "(").replace("}", ")") for line in lines]
    return r"\N".join(clean)


def _ass_header(width: int, height: int, styles: list[str]) -> list[str]:
    return [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        *styles,
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]


def write_segment_ass(cues: list[Cue], scene: Scene, width: int, height: int, path: Path,
                      language: str | None = None) -> Path:
    """Captions for ONE scene, with times counted from the start of that scene.
    language = the caption language (Whisper code); it picks the font (caption_font)."""
    font = caption_font(language, " ".join(" ".join(c.lines) for c in cues if c.scene_index == scene.index))
    font_size = round(min(height * 0.055, width * 0.045) * font_scale(font))  # ~42 characters across 1080 px
    outline = max(2, font_size // 14)
    # White bold text, black outline, bottom centre (Alignment 2).
    lines = _ass_header(width, height, [
        f"Style: Default,{font},{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,"
        f"0,0,1,{outline},1,2,{round(width * 0.05)},{round(width * 0.05)},{round(height * 0.06)},1"])
    for cue in cues:
        if cue.scene_index != scene.index:
            continue
        lines.append(f"Dialogue: 0,{_ass_time(cue.start - scene.start)},{_ass_time(cue.end - scene.start)},"
                     f"Default,,0,0,0,,{_ass_text(cue.lines)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_card_ass(card: dict, width: int, height: int, seconds: float, path: Path) -> Path:
    """The text of a generated card: a big headline + a small label, both exactly as given.
    Each part gets the font of its own script (a Tamil label -> Noto Sans Tamil; "40%" -> CAPTION_FONT).

    The headline fades in and grows very slightly. There is NO count-up animation: counting 0% -> 40%
    drew numbers the narration never says (a poster frame at 0.4 s showed "21%")."""
    headline, label = str(card.get("headline") or ""), str(card.get("label") or "")
    head_font, label_font = caption_font(None, headline), caption_font(None, label)
    head_size = round(min(width, height) * 0.2 * font_scale(head_font))
    label_size = round(min(width, height) * 0.065 * font_scale(label_font))
    cx, head_y = width // 2, round(height * 0.44)
    label_y = head_y + round(head_size * 0.75)
    lines = _ass_header(width, height, [
        f"Style: Head,{head_font},{head_size},&H00FFFFFF,&H000000FF,&H00000000,&H60000000,-1,0,0,0,100,100,"
        f"0,0,1,0,4,5,0,0,0,1",
        f"Style: Label,{label_font},{label_size},&H00F0F0F0,&H000000FF,&H00000000,&H60000000,0,0,0,0,100,100,"
        f"2,0,1,0,2,5,0,0,0,1"])
    end = _ass_time(seconds + 1)
    lines.append(f"Dialogue: 0,0:00:00.00,{end},Head,,0,0,0,,{{\\pos({cx},{head_y})\\fad(300,0)"
                 f"\\t(0,{round(seconds * 1000)},\\fscx106\\fscy106)}}{_ass_text([headline])}")
    if label:
        lines.append(f"Dialogue: 0,0:00:00.30,{end},Label,,0,0,0,,{{\\pos({cx},{label_y})\\fad(500,0)}}"
                     f"{_ass_text([label])}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_badge_ass(badge: dict, width: int, height: int, seconds: float, path: Path) -> Path:
    """A statistic's number as a clean badge over real footage: a dark box at the lower left, above the
    captions; the figure big, its phrase small, both exactly as given (narration only).
    It fades in at 0.3 s and stays; no count-up (that would show numbers nobody said)."""
    headline, label = str(badge.get("headline") or ""), str(badge.get("subtitle") or "")
    head_font, label_font = caption_font(None, headline), caption_font(None, label)
    head_size = round(min(width, height) * 0.085 * font_scale(head_font))
    label_size = round(min(width, height) * 0.038 * font_scale(label_font))
    x, y = round(width * 0.06), round(height * (0.58 if height <= width else 0.62))
    pad = max(8, head_size // 5)
    # BorderStyle 3 = an opaque box behind the text (BackColour &H50101010 = dark grey, ~70% opaque).
    lines = _ass_header(width, height, [
        f"Style: Badge,{head_font},{head_size},&H00FFFFFF,&H000000FF,&H50101010,&H50101010,-1,0,0,0,100,100,"
        f"0,0,3,{pad},0,7,0,0,0,1",
        f"Style: BadgeLabel,{label_font},{label_size},&H00F0F0F0,&H000000FF,&H50101010,&H50101010,0,0,0,0,100,100,"
        f"1,0,3,{pad},0,7,0,0,0,1"])
    end = _ass_time(seconds + 1)
    lines.append(f"Dialogue: 0,0:00:00.30,{end},Badge,,0,0,0,,{{\\pos({x},{y})\\fad(350,0)}}{_ass_text([headline])}")
    if label:
        lines.append(f"Dialogue: 0,0:00:00.45,{end},BadgeLabel,,0,0,0,,{{\\pos({x},{y + head_size + 2 * pad})"
                     f"\\fad(450,0)}}{_ass_text([label])}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------- Looking at a clip (once per clip, shared by all aspect ratios) ----------

@dataclass
class SourceInfo:
    kind: str                     # video | image | generated
    path: Path | None
    width: int = 0
    height: int = 0
    duration: float | None = None
    start: float = 0.0            # where we start reading the clip (seconds)
    col_energy: np.ndarray | None = None   # "how interesting" each column is (left -> right)
    row_energy: np.ndarray | None = None   # same for rows (top -> bottom)
    overlay: dict | None = None            # number badge of a statistic line (rank.overlay_card)
    luma: float | None = None     # mean brightness 0-255 (signalstats YAVG)
    saturation: float | None = None  # mean colour strength (signalstats SATAVG)
    card: dict | None = None
    uid: str = ""
    # Subject-aware crop: small colour frames + the scene meaning + a scorer that can look at pictures.
    crop_frames: list[np.ndarray] | None = None
    crop_texts: list[str] = field(default_factory=list)
    scorer: object | None = None


def _start_offset(duration: float, seconds: float) -> float:
    """Skip up to 1 s of the start (fades, shaky first frames) when the clip is long enough."""
    if duration >= seconds + 0.1:
        return round(min(1.0, (duration - seconds) / 2), 3)
    return 0.0


def _grab_frames(info: SourceInfo, seconds: float, width: int, count: int, colour: bool) -> np.ndarray:
    """`count` small frames spread over the part of the clip we will use.
    Array (frames, rows, cols) for grey, (frames, rows, cols, 3) for colour (RGB)."""
    w = width
    h = max(2, round(w * info.height / max(1, info.width)))
    channels = 3 if colour else 1
    scale = f"scale={w}:{h},format={'rgb24' if colour else 'gray'}"
    if info.kind == "image":
        raw = media.ffmpeg_bytes("-i", str(info.path), "-vf", scale, "-frames:v", "1", "-f", "rawvideo", "pipe:1")
    else:
        span = max(0.5, min(seconds, (info.duration or seconds) - info.start))
        count = max(1, count)
        raw = media.ffmpeg_bytes("-ss", f"{info.start:.3f}", "-t", f"{span:.3f}", "-i", str(info.path),
                                 "-vf", f"fps={count / span:.4f},{scale}", "-frames:v", str(count),
                                 "-f", "rawvideo", "pipe:1")
    frames = np.frombuffer(raw, dtype=np.uint8)
    size = w * h * channels
    got = len(frames) // size
    if got == 0:
        raise media.MediaError("no frames could be sampled")
    shape = (got, h, w, 3) if colour else (got, h, w)
    return frames[: got * size].reshape(shape)


def _sample_frames(info: SourceInfo, seconds: float) -> np.ndarray:
    """A few small grey frames for the edge + motion energy: array (frames, rows, cols)."""
    count = get_settings().SALIENCY_SAMPLE_FRAMES
    return _grab_frames(info, seconds, ANALYSIS_WIDTH, count, colour=False).astype(np.float32)


def energy_profiles(frames: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Edges (sharp detail) + motion (what changes between frames) -> per-column and per-row totals.

    The subject of a shot usually has the most detail and movement; flat sky or a blurred
    background has little.
    """
    dx = np.abs(np.diff(frames, axis=2))[:, :-1, :]   # change left -> right
    dy = np.abs(np.diff(frames, axis=1))[:, :, :-1]   # change top -> bottom
    energy = (dx + dy).mean(axis=0)
    if len(frames) > 1:
        motion = np.abs(np.diff(frames, axis=0)).mean(axis=0)[:-1, :-1]
        energy = energy + 2 * motion
    return energy.sum(axis=0), energy.sum(axis=1)


def _signal_stats(info: SourceInfo, seconds: float) -> tuple[float | None, float | None]:
    """Mean YAVG (brightness) and SATAVG (colour) measured by FFmpeg's signalstats filter."""
    if info.kind == "image":
        args = ["-i", str(info.path), "-vf", "scale=320:-2,signalstats,metadata=mode=print", "-frames:v", "1"]
    else:
        span = max(0.5, min(seconds, (info.duration or seconds) - info.start))
        args = ["-ss", f"{info.start:.3f}", "-t", f"{span:.3f}", "-i", str(info.path),
                "-vf", "fps=2,scale=320:-2,signalstats,metadata=mode=print"]
    out = media.ffmpeg(*args, "-an", "-f", "null", "-", loglevel="info").stderr
    lumas = [float(x) for x in re.findall(r"lavfi\.signalstats\.YAVG=([\d.]+)", out)]
    sats = [float(x) for x in re.findall(r"lavfi\.signalstats\.SATAVG=([\d.]+)", out)]
    return (sum(lumas) / len(lumas) if lumas else None), (sum(sats) / len(sats) if sats else None)


def analyse_source(selection: Selection, seconds: float, texts: list[str] | None = None,
                   scorer=None) -> SourceInfo:
    """Look at the clip once (shared by all aspect ratios). With `texts` (the scene meaning) and a
    scorer that can look at pictures (CLIP), colour frames are kept for the subject-aware crop."""
    c = selection.candidate
    if c.kind == "generated":
        return SourceInfo(kind="generated", path=None, card=c.extra, uid=c.uid)
    probe = media.probe(selection.path) or {}
    stream = next((st for st in probe.get("streams", []) if st.get("codec_type") == "video"), {})
    info = SourceInfo(kind=c.kind, path=selection.path, width=int(stream.get("width") or c.width or 0),
                      height=int(stream.get("height") or c.height or 0), uid=c.uid,
                      overlay=getattr(selection, "overlay", None))
    if c.kind == "video":
        info.duration = float(probe.get("format", {}).get("duration") or c.duration or seconds)
        info.start = _start_offset(info.duration, seconds)
    try:
        frames = _sample_frames(info, seconds)
        info.col_energy, info.row_energy = energy_profiles(frames)
    except media.MediaError as e:
        log.warning("saliency analysis failed for %s: %s", c.uid, e)
    if texts and scorer is not None and getattr(scorer, "needs_images", False):
        try:
            info.crop_frames = list(_grab_frames(info, seconds, CROP_ANALYSIS_WIDTH,
                                                 get_settings().CROP_SAMPLE_FRAMES, colour=True))
            info.crop_texts, info.scorer = list(texts), scorer
        except media.MediaError as e:
            log.warning("crop frames could not be sampled for %s: %s", c.uid, e)
    try:
        info.luma, info.saturation = _signal_stats(info, seconds)
    except media.MediaError as e:
        log.warning("signalstats failed for %s: %s", c.uid, e)
    return info


# ---------- Where to crop ----------

def choose_window(profile: np.ndarray | None, fraction: float) -> tuple[float, float, str]:
    """Pick a window that covers `fraction` of the profile's length.

    Returns (start as a fraction of the length, subject centre fraction, mode):
    - "center":  no clear subject (energy is even) -> just take the middle,
    - "salient": the subject fits -> centre the window on it,
    - "blur":    the subject is wider than the window -> caller shows the whole frame on blur.
    The subject = the energy ABOVE the typical (median) level of the profile.
    """
    fraction = min(1.0, max(0.0, fraction))
    if profile is None or len(profile) < 4 or profile.sum() <= 0:
        return (1 - fraction) / 2, 0.5, "center"
    n = len(profile)
    excess = np.clip(profile - np.median(profile), 0, None)
    if excess.sum() < 0.15 * profile.sum():
        return (1 - fraction) / 2, 0.5, "center"
    cumulative = np.cumsum(excess) / excess.sum()
    lo, hi = int(np.searchsorted(cumulative, 0.10)), int(np.searchsorted(cumulative, 0.90))
    centre = float((np.arange(n) * excess).sum() / excess.sum()) / n
    if (hi - lo + 1) / n > fraction * 1.25:
        return (1 - fraction) / 2, centre, "blur"
    start = centre - fraction / 2
    if (hi + 1 - lo) / n <= fraction:  # the subject fits: hold all of it, even at the far edge
        start = min(max(start, (hi + 1) / n - fraction), lo / n)
    start = min(max(start, 0.0), 1.0 - fraction)
    return start, centre, "salient"


@dataclass
class Framing:
    mode: str           # fit | center | salient | subject | blur
    x: float = 0.0      # crop box as fractions of the source frame
    y: float = 0.0
    w: float = 1.0
    h: float = 1.0
    focus_x: float = 0.5  # subject centre inside the cropped picture (for Ken Burns)
    focus_y: float = 0.5
    crop: dict | None = None  # how the crop was chosen (for timeline.json)
    end: float | None = None  # following a moving subject: the window start (x or y) at the LAST frame


def _pad_square(picture: np.ndarray) -> np.ndarray:
    """Pad to a square with the picture's mean colour. CLIP cuts the middle square out of
    every picture; padding first means it sees the WHOLE window (or the whole frame)."""
    h, w = picture.shape[:2]
    side = max(h, w)
    out = np.empty((side, side, 3), dtype=np.uint8)
    out[:] = picture.reshape(-1, 3).mean(axis=0).astype(np.uint8)
    top, left = (side - h) // 2, (side - w) // 2
    out[top:top + h, left:left + w] = picture
    return out


def _cut(frame: np.ndarray, axis: str, start: float, frac: float) -> np.ndarray:
    """The part of `frame` from `start` to `start + frac` (fractions) along the crop axis."""
    length = frame.shape[1] if axis == "x" else frame.shape[0]
    size = max(1, round(length * frac))
    a = min(max(0, round(start * length)), length - size)
    return frame[:, a:a + size] if axis == "x" else frame[a:a + size, :]


def _smooth(row: np.ndarray) -> np.ndarray:
    """Light smoothing (1/4, 1/2, 1/4): removes single-strip CLIP noise but keeps a narrow subject's peak."""
    padded = np.pad(np.asarray(row, dtype=np.float64), 1, mode="edge")
    return 0.25 * padded[:-2] + 0.5 * padded[1:-1] + 0.25 * padded[2:]


def _strongest_run(row: np.ndarray, centres: np.ndarray, strip: float) -> list[float]:
    """[centre, contrast, span start, span end] of the strongest run of strips in one (smoothed) profile:
    the peak and its neighbours that stay at least halfway between the median and the peak; centre = their
    contrast-weighted centre; span = the run widened by half a strip on each side."""
    r = _smooth(row)
    med, p = float(np.median(r)), int(r.argmax())
    contrast = float(r[p]) - med
    a = b = p
    while a > 0 and r[a - 1] >= med + contrast / 2:
        a -= 1
    while b < len(r) - 1 and r[b + 1] >= med + contrast / 2:
        b += 1
    w = r[a:b + 1] - med
    centre = float((centres[a:b + 1] * w).sum() / w.sum()) if w.sum() > 0 else float(centres[p])
    return [round(centre, 4), round(contrast, 4), round(max(0.0, float(centres[a]) - strip / 2), 4),
            round(min(1.0, float(centres[b]) + strip / 2), 4)]


def locate_subject(profiles: np.ndarray, centres: np.ndarray, strip: float, min_contrast: float) -> dict:
    """Where is the line's subject along the crop axis?

    profiles = CLIP scores of narrow strips, one row per sampled frame; centres = each strip's centre (0-1).
    The subject = the strongest run of strips clearly above the typical (median) strip (_strongest_run), found when
    its peak is at least min_contrast above the median: on the frames' AVERAGE (a subject that stays put), or in
    EVERY frame on its own (a subject that moves: on the average it is smeared out). Also per frame:
    [centre, contrast, span start, span end], to follow a moving subject."""
    per_frame = [_strongest_run(row, centres, strip) for row in profiles]
    centre, contrast, lo, hi = _strongest_run(profiles.mean(axis=0), centres, strip)
    if contrast < min_contrast:
        if len(per_frame) < 2 or min(f[1] for f in per_frame) < min_contrast:
            return {"found": False, "contrast": contrast}
        # Clear in every frame but smeared on the average: it moves. Span = all its positions together.
        centre = round(float(np.mean([f[0] for f in per_frame])), 4)
        lo, hi = min(f[2] for f in per_frame), max(f[3] for f in per_frame)
        contrast = min(f[1] for f in per_frame)
    return {"found": True, "contrast": contrast, "centre": centre, "span": [lo, hi], "per_frame": per_frame}


def place_window(span: tuple[float, float], centre: float, frac: float) -> float:
    """Start of a window of size `frac` centred on the subject, moved just enough to hold the WHOLE span when the
    span fits (a subject at the far edge stays fully inside), always inside the frame."""
    lo, hi = span
    start = centre - frac / 2
    if hi - lo <= frac:
        start = min(max(start, hi - frac), lo)
    return round(min(max(start, 0.0), 1.0 - frac), 4)


def _clip_crop(info: SourceInfo, axis: str, frac: float):
    """CLIP-guided crop. Returns (choice, start, end, facts) or None if scoring was not possible.

    1. Locate: CROP_STRIPS narrow strips (CROP_STRIP_FRACTION of the window wide) across EVERY sampled frame are
       scored with CLIP against the scene meaning -> locate_subject().
    2. Place: the window holds the whole subject (place_window). If the subject's centre moves by at least
       CROP_FOLLOW_MIN_SHIFT between the first and last sampled frame, the window pans smoothly from where it
       is at the start to where it is at the end ("follow").
    3. No clear subject (flat strip scores, e.g. a wide landscape): centre crop + a warning.
    4. Check: if the chosen window scores more than CROP_BLUR_MARGIN below the whole frame, it loses the subject
       (e.g. two people at both edges) -> blurred fit (the whole frame on a blurred copy of itself)."""
    s = get_settings()
    frames = info.crop_frames or []
    if not frames:
        return None
    strip = min(1.0, frac * s.CROP_STRIP_FRACTION)
    count = max(3, s.CROP_STRIPS)
    centres = np.linspace(0.0, 1.0 - strip, count) + strip / 2
    pictures = []
    for frame in frames:
        pictures.append(_pad_square(frame))
        pictures += [_pad_square(_cut(frame, axis, c - strip / 2, strip)) for c in centres]
    try:
        scores = info.scorer.score_images(pictures, info.crop_texts)
    except Exception as e:  # a crop check problem must never fail the render
        log.warning("crop scoring failed for %s: %s", info.uid, e)
        return None
    if not scores:
        return None  # this scorer cannot look at pictures
    table = np.asarray(scores, dtype=np.float64).reshape(len(frames), count + 1)
    full = round(float(table[:, 0].mean()), 4)
    subject = locate_subject(table[:, 1:], centres, strip, s.CROP_SUBJECT_MIN_CONTRAST)
    warning = None
    if not subject["found"]:
        start = end = round((1 - frac) / 2, 4)
        choice = "centre"
        warning = (f"no clear subject found (CLIP strip contrast {subject['contrast']:.3f} < "
                   f"CROP_SUBJECT_MIN_CONTRAST {s.CROP_SUBJECT_MIN_CONTRAST:g}): centre crop")
        reason = warning
    else:
        lo, hi = subject["span"]
        start = end = place_window((lo, hi), subject["centre"], frac)
        choice = "subject"
        per_frame = subject["per_frame"]
        path = [f[0] for f in per_frame]
        steps_moved = np.diff(path)
        one_way = bool(len(steps_moved)) and (bool((steps_moved >= 0).all()) or bool((steps_moved <= 0).all()))
        if (len(frames) > 1 and one_way and abs(path[-1] - path[0]) >= s.CROP_FOLLOW_MIN_SHIFT
                and min(f[1] for f in per_frame) >= s.CROP_SUBJECT_MIN_CONTRAST):
            # Each end of the pan holds that frame's own subject. The frames were sampled at i/count of the clip
            # part we use (i = 0 .. count-1), so the pan is stretched to reach the segment's last frame.
            first = place_window(per_frame[0][2:4], path[0], frac)
            last = place_window(per_frame[-1][2:4], path[-1], frac)
            count_f = len(frames)
            start = first
            end = round(min(max(first + (last - first) * count_f / (count_f - 1), 0.0), 1.0 - frac), 4)
            choice = "follow" if abs(end - start) > 1e-4 else "subject"
        inside = start <= lo + 1e-4 and hi <= start + frac + 1e-4
        reason = (f"subject at {lo:.2f}-{hi:.2f} (centre {subject['centre']:.2f}, CLIP strip contrast "
                  f"{subject['contrast']:.3f}) -> window {start:.2f}-{start + frac:.2f}"
                  + (f", panning to {end:.2f}-{end + frac:.2f} as it moves" if choice == "follow" else "")
                  + (" (whole subject inside)" if inside and choice != "follow" else ""))
    # The window where each sampled frame will see it (frame i was sampled at i / count of the clip part used).
    window_pictures = [_pad_square(_cut(f, axis, start + (end - start) * i / len(frames), frac))
                       for i, f in enumerate(frames)]
    try:
        window = round(float(np.mean(info.scorer.score_images(window_pictures, info.crop_texts))), 4)
    except Exception as e:  # keep the crop; only the blur check is skipped
        log.warning("crop window scoring failed for %s: %s", info.uid, e)
        window = full
    if window < full - s.CROP_BLUR_MARGIN:
        choice = "blur"
        reason = (f"the chosen window scores {window:.3f}, more than {s.CROP_BLUR_MARGIN:g} below the whole frame "
                  f"{full:.3f}: every window cuts the subject -> blurred fit")
    facts = {"method": "clip", "choice": "blurred fit" if choice == "blur" else choice, "axis": axis,
             "window_fraction": round(frac, 4), "start": start, "end": end, "window_score": window,
             "full_frame_score": full, "frames": len(frames), "strips": count, "strip_fraction": round(strip, 4),
             "subject": ({k: subject[k] for k in ("span", "centre", "contrast", "per_frame")} if subject["found"]
                         else None),
             "contrast": subject["contrast"], "blur_margin": s.CROP_BLUR_MARGIN, "reason": reason}
    if warning and choice != "blur":
        facts["warning"] = warning
    return choice, start, end, facts


def plan_framing(info: SourceInfo, width: int, height: int) -> Framing:
    """How to get from the clip's shape to the output shape."""
    target = width / height
    source = info.width / info.height if info.width and info.height else target
    if abs(source - target) / target < 0.02:
        return Framing("fit", crop={"method": "none", "choice": "fit (same shape)"})
    axis = "x" if source > target else "y"  # x: clip is wider, cut away left/right; y: cut top/bottom
    frac = target / source if axis == "x" else source / target
    profile, other = (info.col_energy, info.row_energy) if axis == "x" else (info.row_energy, info.col_energy)
    start, centre, mode = choose_window(profile, frac)
    _, other_centre, _ = choose_window(other, 1.0)
    end = None
    clip = _clip_crop(info, axis, frac) if info.scorer else None
    if clip:
        choice, start, end, facts = clip
        if choice == "blur":
            return Framing("blur", crop=facts)
        mode = "subject"
        centre = (facts["subject"] or {}).get("centre", start + frac / 2)
        if end == start:
            end = None
    else:
        facts = {"method": "saliency", "choice": {"blur": "blurred fit"}.get(mode, mode),
                 "window_fraction": round(frac, 4), "start": round(start, 4)}
        if mode == "center":
            facts["warning"] = "no clear subject found (edge + motion energy is even): centre crop"
        if mode == "blur":
            return Framing("blur", crop=facts)
    focus = min(max((centre - start) / frac, 0.0), 1.0)
    if axis == "x":
        return Framing(mode, x=round(start, 4), w=round(frac, 4), focus_x=focus, focus_y=other_centre, crop=facts,
                       end=end)
    return Framing(mode, y=round(start, 4), h=round(frac, 4), focus_x=other_centre, focus_y=focus, crop=facts,
                   end=end)


# ---------- Look (brightness / colour) ----------

def look_filter(info: SourceInfo) -> str | None:
    """eq filter that moves this clip's brightness/colour toward the common target, or None."""
    s = get_settings()
    parts = []
    if info.luma is not None:
        shift = max(-s.MAX_BRIGHTNESS_SHIFT, min(s.MAX_BRIGHTNESS_SHIFT, (s.TARGET_LUMA - info.luma) / 255))
        if abs(shift) >= 0.01:
            parts.append(f"brightness={shift:.3f}")
    if info.saturation and info.saturation > 1:
        factor = max(0.85, min(1.2, s.TARGET_SATURATION / info.saturation))
        if abs(factor - 1) >= 0.02:
            parts.append(f"saturation={factor:.3f}")
    return f"eq={':'.join(parts)}" if parts else None


# ---------- One segment ----------

def frame_counts(scenes: list[Scene], fps: int) -> list[int]:
    """Whole frames per scene, from the scene times. Sum = round(total length * fps)."""
    counts = [round(sc.end * fps) - round(sc.start * fps) for sc in scenes]
    return [max(1, c) for c in counts]


def segment_key(info: SourceInfo, scene: Scene, ratio: str, cues: list[Cue], burn_captions: bool,
                language: str | None = None) -> str:
    """Same key = same picture, so the cached segment can be reused."""
    s = get_settings()
    caption_text = [(c.start, c.end, c.lines) for c in cues if c.scene_index == scene.index] if burn_captions else []
    font = caption_font(language, json.dumps(caption_text, ensure_ascii=False))
    data = [info.uid, scene.start, scene.end, ratio, caption_text, s.RENDER_FPS, s.VIDEO_PRESET, s.VIDEO_CRF,
            s.TARGET_LUMA, s.TARGET_SATURATION, s.KEN_BURNS_ZOOM, s.MAX_SLOWDOWN, font, font_scale(font),
            # The crop depends on the scene meaning and on whether CLIP looked at it.
            info.crop_texts, getattr(info.scorer, "name", None), s.CROP_SAMPLE_FRAMES, s.CROP_BLUR_MARGIN,
            s.CROP_STRIPS, s.CROP_STRIP_FRACTION, s.CROP_SUBJECT_MIN_CONTRAST, s.CROP_FOLLOW_MIN_SHIFT, info.overlay]
    return hashlib.sha1(json.dumps(data, default=str).encode()).hexdigest()[:16]


def _encode_args() -> list[str]:
    s = get_settings()
    return ["-c:v", "libx264", "-preset", s.VIDEO_PRESET, "-crf", str(s.VIDEO_CRF), "-pix_fmt", "yuv420p",
            "-r", str(s.RENDER_FPS)]


def _pingpong(info: SourceInfo, folder: Path) -> Path:
    """Slow the clip by MAX_SLOWDOWN and append it backwards: loops with no jump and no freeze."""
    s = get_settings()
    out = folder / f"pingpong_{hashlib.sha1(info.uid.encode()).hexdigest()[:10]}.mp4"
    if out.exists():
        return out
    width = min(1920, info.width or 1920) // 2 * 2  # reversing keeps frames in memory: cap the size
    graph = (f"[0:v]scale={width}:-2,setpts={1 + s.MAX_SLOWDOWN:.4f}*PTS,fps={s.RENDER_FPS},split[a][b];"
             f"[b]reverse[r];[a][r]concat=n=2:v=1:a=0[v]")
    media.ffmpeg("-i", str(info.path.resolve()), "-filter_complex", graph, "-map", "[v]", "-an",
                 "-c:v", "libx264", "-preset", s.VIDEO_PRESET, "-crf", "18", "-pix_fmt", "yuv420p", out.name,
                 cwd=folder)
    return out


def time_plan(duration: float, seconds: float, max_slowdown: float) -> tuple[str, float]:
    """("direct", 1.0) | ("slow", factor) | ("loop", factor). factor = how much longer the clip plays."""
    if duration >= seconds + 0.1:
        return "direct", 1.0
    factor = (seconds + 0.1) / max(duration, 0.01)  # +0.1 s so we never run out of frames
    if factor <= 1 + max_slowdown:
        return "slow", round(factor, 4)
    return "loop", round(1 + max_slowdown, 4)


def _frame_chain(framing: Framing, width: int, height: int, frames: int = 1) -> tuple[str, str]:
    """(filter graph from [t] to [f], description). [t] = time-fitted frames, [f] = output size.
    A following crop moves its window linearly from framing.x / y (first frame) to framing.end (last frame)."""
    if framing.mode == "blur":
        sw, sh = width // 4, height // 4
        return (f"[t]split[a][b];[a]scale={sw}:{sh}:force_original_aspect_ratio=increase,crop={sw}:{sh},"
                f"boxblur=8:2,scale={width}:{height}[bg];[b]scale={width}:{height}:force_original_aspect_ratio="
                f"decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2[f]", "blurred fit")
    x, y = f"iw*{framing.x}", f"ih*{framing.y}"
    if framing.end is not None and frames > 1:  # n = output frame number (0 .. frames - 1), after the fps filter
        if framing.w < 1:
            x = f"'iw*({framing.x}+{framing.end - framing.x:.4f}*n/{frames - 1})'"
        else:
            y = f"'ih*({framing.y}+{framing.end - framing.y:.4f}*n/{frames - 1})'"
    crop = "" if framing.mode == "fit" else f"crop=w=iw*{framing.w}:h=ih*{framing.h}:x={x}:y={y},"
    described = f"subject crop ({framing.crop['choice']})" if framing.mode == "subject" else framing.mode
    return f"[t]{crop}scale={width}:{height}:flags=lanczos[f]", described


def render_segment(info: SourceInfo, scene: Scene, ratio: str, cues: list[Cue], folder: Path, frames: int,
                   burn_captions: bool, language: str | None = None) -> tuple[Path, dict]:
    """Render (or reuse) the segment for one scene + ratio. Returns (path, facts for timeline.json).
    language = the caption language (picks the caption / card font)."""
    s = get_settings()
    width, height = RESOLUTIONS[ratio]
    fps = s.RENDER_FPS
    seconds = frames / fps
    key = segment_key(info, scene, ratio, cues, burn_captions, language)
    name = f"seg_{scene.index:03d}_{key}.mp4"
    facts_file = folder / f"seg_{scene.index:03d}_{key}.json"  # the facts, so a cached segment keeps them
    if (folder / name).exists() and facts_file.exists():
        return folder / name, {**json.loads(facts_file.read_text(encoding="utf-8")), "cached": True}
    facts = {"file": name, "cache_key": key, "frames": frames}
    finish = []  # filters after framing: look, captions, pixel format
    if info.kind != "generated" and (look := look_filter(info)):
        finish.append(look)
        facts["look"] = look
    if info.kind == "generated":
        write_card_ass(info.card or {}, width, height, seconds, folder / f"card_{scene.index:03d}.ass")
        finish.append(subtitles_filter(f"card_{scene.index:03d}.ass", folder))
    elif info.overlay:
        badge = write_badge_ass(info.overlay, width, height, seconds,
                                folder / f"badge_{scene.index:03d}_{ratio_slug(ratio)}.ass")
        finish.append(subtitles_filter(badge.name, folder))
        facts["overlay"] = {"headline": info.overlay.get("headline"), "subtitle": info.overlay.get("subtitle"),
                            "source": "narration"}
    if burn_captions and scene.words:
        ass = write_segment_ass(cues, scene, width, height, folder / f"seg_{scene.index:03d}_{ratio_slug(ratio)}.ass",
                                language)
        finish.append(subtitles_filter(ass.name, folder))  # bare names: Windows paths break filter syntax
        facts["caption_font"] = caption_font(language, " ".join(" ".join(c.lines) for c in cues
                                                                 if c.scene_index == scene.index))
    # JPEG photos are full range ("yuvj420p"); convert every segment to normal limited (tv) range
    # so all segments match and players show the right brightness.
    finish.append("setsar=1,scale=out_range=tv,format=yuv420p")
    tail = ",".join(finish)

    if info.kind == "generated":
        c0, c1 = (info.card or {}).get("palette", ["0x355C7D", "0x6C5B7B"])
        source = (f"gradients=s={width}x{height}:r={fps}:c0={c0}:c1={c1}:x0=0:y0=0:x1={width}:y1={height}:"
                  f"speed=0.03:seed={s.SEED}:d={seconds + 1:.3f}")
        inputs, graph = ["-f", "lavfi", "-i", source], f"[0:v]{tail}[v]"
        facts.update(timing="generated", framing="card")
    elif info.kind == "image":
        framing = plan_framing(info, width, height)
        frame_graph, facts["framing"] = _frame_chain(framing, width * 2, height * 2)  # 2x canvas: smooth zoom
        facts["crop"] = framing.crop
        z, n = s.KEN_BURNS_ZOOM, max(1, frames - 1)
        fx, fy = min(max(framing.focus_x, 0.0), 1.0), min(max(framing.focus_y, 0.0), 1.0)
        # Zoom from 1.0 to KEN_BURNS_ZOOM while drifting from the centre toward the subject.
        zoom = (f"zoompan=z='1+{z - 1:.4f}*on/{n}':x='(iw-iw/zoom)*(0.5+{fx - 0.5:.4f}*on/{n})'"
                f":y='(ih-ih/zoom)*(0.5+{fy - 0.5:.4f}*on/{n})':d={frames}:s={width}x{height}:fps={fps}")
        inputs = ["-i", str(info.path.resolve())]
        graph = f"[0:v]format=yuv420p[t];{frame_graph};[f]{zoom},{tail}[v]"
        facts["timing"] = f"ken burns zoom to {z:g}x"
    else:
        mode, factor = time_plan(info.duration or 0.0, seconds, s.MAX_SLOWDOWN)
        framing = plan_framing(info, width, height)
        frame_graph, facts["framing"] = _frame_chain(framing, width, height, frames)
        facts["crop"] = framing.crop
        if mode == "loop":
            inputs = ["-stream_loop", "-1", "-i", str(_pingpong(info, folder).resolve())]
            time_filters = f"fps={fps}"
            facts["timing"] = f"slowed {factor:g}x + ping-pong loop"
        else:
            inputs = (["-ss", f"{info.start:.3f}"] if mode == "direct" else []) + ["-i", str(info.path.resolve())]
            time_filters = f"setpts={factor}*PTS,fps={fps}" if mode == "slow" else f"fps={fps}"
            facts["timing"] = "trimmed" if mode == "direct" else f"slowed {factor:g}x"
        graph = f"[0:v]{time_filters}[t];{frame_graph};[f]{tail}[v]"
    media.ffmpeg(*inputs, "-filter_complex", graph, "-map", "[v]", "-frames:v", str(frames), *_encode_args(),
                 "-an", name, cwd=folder)
    facts_file.write_text(json.dumps(facts), encoding="utf-8")
    return folder / name, facts


def concat_segments(segment_paths: list[Path], narration: Path, folder: Path, out_path: Path) -> None:
    """Join the segments with stream copy (no re-encoding) and add the narration as AAC."""
    s = get_settings()
    (folder / "segments.txt").write_text("".join(f"file '{p.name}'\n" for p in segment_paths), encoding="utf-8")
    media.ffmpeg("-f", "concat", "-safe", "0", "-i", "segments.txt", "-i", str(narration.resolve()),
                 "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
                 "-c:a", "aac", "-b:a", s.AUDIO_BITRATE, "-ar", str(s.AUDIO_SAMPLE_RATE), "-ac", "2",
                 "-movflags", "+faststart", str(out_path.resolve()), cwd=folder)


THUMBNAIL_SIZE = (640, 360)  # small 16:9 poster frame per scene (for the Studio's scene cards)


def thumbnail_filename(scene_index: int) -> str:
    return f"thumb_{scene_index:03d}.jpg"


def save_thumbnail(segment: Path, out_path: Path, seconds: float) -> None:
    """One JPEG poster frame of a rendered segment, always 16:9 (a 9:16 / 1:1 segment is padded).

    "First good frame": start a little after the cut (0.3 s, or the middle of a very short scene), then
    ffmpeg's `thumbnail` filter picks the most typical of the next 10 frames (skips a flash or fade).
    Raises media.MediaError if ffmpeg fails.
    """
    w, h = THUMBNAIL_SIZE
    media.ffmpeg("-ss", f"{min(0.3, seconds / 2):.3f}", "-i", str(segment.resolve()), "-vf",
                 f"thumbnail=n=10,scale={w}:{h}:force_original_aspect_ratio=decrease,"
                 f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=0x111111,format=yuvj420p",
                 "-frames:v", "1", "-q:v", "4", str(out_path.resolve()))


def render_failed(ratio: str, e: media.MediaError) -> AppError:
    log.error("render failed for %s: %s | %s", ratio, e, e.output)
    return AppError(ErrorCode.RENDER_FAILED, f"Rendering the {ratio} video failed", 500, retryable=True)
