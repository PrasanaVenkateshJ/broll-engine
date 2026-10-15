"""Make the final video for one aspect ratio.

FIRST VERSION (Phase 4): each scene is a plain coloured placeholder clip. Real B-roll
footage replaces the colour in Phase 6/7; the rest of the flow stays the same:

1. One segment per scene: exact length, 30 fps, yuv420p, H.264, captions burned in.
   (Separate segments mean a later "swap this clip" only re-renders one segment.)
2. Join the segments with stream copy (no re-encoding) and add the narration as AAC.

Segment lengths are counted in whole frames from the scene times, so the total video
length stays within one frame of the narration.
"""
import logging
from pathlib import Path

from app.config import get_settings
from app.errors import AppError, ErrorCode
from app.pipeline import media
from app.pipeline.outputs import Cue
from app.pipeline.scenes import Scene

log = logging.getLogger("app.pipeline.render")

RESOLUTIONS = {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080)}
# Calm placeholder colours, one per scene in turn (FFmpeg colour syntax 0xRRGGBB).
PLACEHOLDER_COLORS = ["0x2E4057", "0x4F6D7A", "0x56A3A6", "0x8D6A9F", "0xC17C74", "0x6B8F71"]


def ratio_slug(ratio: str) -> str:
    """"16:9" -> "16x9" (safe in file names)."""
    return ratio.replace(":", "x")


def video_filename(ratio: str) -> str:
    return f"video_{ratio_slug(ratio)}.mp4"


def placeholder_color(scene_index: int) -> str:
    return PLACEHOLDER_COLORS[scene_index % len(PLACEHOLDER_COLORS)]


# ---------- Burned captions (ASS subtitle format, drawn by FFmpeg's libass) ----------

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


def write_segment_ass(cues: list[Cue], scene: Scene, width: int, height: int, path: Path) -> Path:
    """Captions for ONE scene, with times counted from the start of that scene."""
    s = get_settings()
    font_size = round(min(height * 0.055, width * 0.045))  # fits ~42 characters across a 1080-wide frame
    outline = max(2, font_size // 14)
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        # White bold text, black outline, bottom centre (Alignment 2).
        f"Style: Default,{s.CAPTION_FONT},{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,"
        f"0,0,1,{outline},1,2,{round(width * 0.05)},{round(width * 0.05)},{round(height * 0.06)},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for cue in cues:
        if cue.scene_index != scene.index:
            continue
        lines.append(f"Dialogue: 0,{_ass_time(cue.start - scene.start)},{_ass_time(cue.end - scene.start)},"
                     f"Default,,0,0,0,,{_ass_text(cue.lines)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------- Render ----------

def frame_counts(scenes: list[Scene], fps: int) -> list[int]:
    """Whole frames per scene, from the scene times. Sum = round(total length * fps)."""
    counts = [round(sc.end * fps) - round(sc.start * fps) for sc in scenes]
    return [max(1, c) for c in counts]


def render_video(scenes: list[Scene], cues: list[Cue], narration: Path, ratio: str, work_dir: Path,
                 out_path: Path, burn_captions: bool = True) -> dict:
    """Render one aspect ratio. Returns facts about the file (for the timeline/manifest)."""
    s = get_settings()
    if ratio not in RESOLUTIONS:
        raise AppError(ErrorCode.UNSUPPORTED_ASPECT_RATIO, f"Unsupported aspect ratio {ratio}", 422)
    width, height = RESOLUTIONS[ratio]
    fps = s.RENDER_FPS
    folder = work_dir / f"render_{ratio_slug(ratio)}"
    folder.mkdir(parents=True, exist_ok=True)
    encode = ["-c:v", "libx264", "-preset", s.VIDEO_PRESET, "-crf", str(s.VIDEO_CRF), "-pix_fmt", "yuv420p",
              "-r", str(fps)]

    try:
        names = []
        for scene, frames in zip(scenes, frame_counts(scenes, fps)):
            name = f"seg_{scene.index:03d}.mp4"
            filters = "format=yuv420p"
            if burn_captions and scene.words:
                ass = write_segment_ass(cues, scene, width, height, folder / f"seg_{scene.index:03d}.ass")
                # Run inside `folder` and use the bare file name: Windows paths ("C:\...") break filter syntax.
                filters = f"subtitles=filename={ass.name},format=yuv420p"
            media.ffmpeg("-f", "lavfi", "-i", f"color=c={placeholder_color(scene.index)}:s={width}x{height}:r={fps}",
                         "-frames:v", str(frames), "-vf", filters, *encode, "-an", name, cwd=folder)
            names.append(name)

        (folder / "segments.txt").write_text("".join(f"file '{n}'\n" for n in names), encoding="utf-8")
        media.ffmpeg("-f", "concat", "-safe", "0", "-i", "segments.txt", "-i", str(narration.resolve()),
                     "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
                     "-c:a", "aac", "-b:a", s.AUDIO_BITRATE, "-ar", str(s.AUDIO_SAMPLE_RATE), "-ac", "2",
                     "-movflags", "+faststart", str(out_path.resolve()), cwd=folder)
    except media.MediaError as e:
        log.error("render failed for %s: %s | %s", ratio, e, e.output)
        raise AppError(ErrorCode.RENDER_FAILED, f"Rendering the {ratio} video failed", 500, retryable=True) from None

    return {"aspect_ratio": ratio, "file": out_path.name, "width": width, "height": height, "fps": fps,
            "duration": round(media.duration_of(out_path), 3)}
