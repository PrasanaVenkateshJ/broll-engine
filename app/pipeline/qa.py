"""Quality checks after rendering.

Per segment (so a bad clip can be swapped for another one, see runner.py):
- blackdetect:  black frames for at least QA_BLACK_SECONDS
- freezedetect: the picture does not change for at least QA_FREEZE_SECONDS
- frame count:  the segment has exactly as many frames as its scene needs
Per final video:
- |video length - narration length| < QA_MAX_DRIFT_SECONDS, plus codec / fps / pixel format.
A check returns a list of problems in plain words; an empty list means OK.
"""
import re
from fractions import Fraction
from pathlib import Path

from app.config import get_settings
from app.pipeline import media

BLACK = re.compile(r"black_start:\s*([\d.]+)\s+black_end:\s*([\d.]+)")
FREEZE = re.compile(r"freeze_start:\s*([\d.]+)")


def check_segment(path: Path, expected_frames: int) -> list[str]:
    s = get_settings()
    problems = []
    try:
        out = media.ffmpeg("-i", str(path), "-vf",
                           f"blackdetect=d={s.QA_BLACK_SECONDS}:pix_th=0.10,"
                           f"freezedetect=n=-60dB:d={s.QA_FREEZE_SECONDS}",
                           "-an", "-f", "null", "-", loglevel="info").stderr
    except media.MediaError as e:
        return [f"the segment cannot be decoded ({e})"]
    for start, end in BLACK.findall(out):
        problems.append(f"black frames from {float(start):.2f} s to {float(end):.2f} s")
    for start in FREEZE.findall(out):
        problems.append(f"frozen picture from {float(start):.2f} s")
    info = media.probe(path) or {}
    video = next((st for st in info.get("streams", []) if st.get("codec_type") == "video"), {})
    frames = int(video.get("nb_frames") or 0)
    if frames and frames != expected_frames:
        problems.append(f"{frames} frames instead of {expected_frames}")
    return problems


def check_video(path: Path, audio_seconds: float, width: int, height: int) -> tuple[list[str], dict]:
    """Final video checks. Returns (problems, measured facts for the manifest)."""
    s = get_settings()
    info = media.probe(path) or {}
    streams = info.get("streams", [])
    video = next((st for st in streams if st.get("codec_type") == "video"), {})
    audio = next((st for st in streams if st.get("codec_type") == "audio"), {})
    video_seconds = float(video.get("duration") or info.get("format", {}).get("duration") or 0)
    drift = round(abs(video_seconds - audio_seconds), 3)
    try:
        fps = float(Fraction(video.get("avg_frame_rate") or "0/1"))
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    facts = {"video_seconds": round(video_seconds, 3), "audio_seconds": round(audio_seconds, 3), "drift_seconds": drift,
             "codec": video.get("codec_name"), "pix_fmt": video.get("pix_fmt"), "fps": round(fps, 3),
             "audio_codec": audio.get("codec_name"), "audio_sample_rate": audio.get("sample_rate")}
    problems = []
    if drift >= s.QA_MAX_DRIFT_SECONDS:
        problems.append(f"video and narration lengths differ by {drift:.3f} s")
    if video.get("codec_name") != "h264" or video.get("pix_fmt") != "yuv420p":
        problems.append(f"video is {video.get('codec_name')}/{video.get('pix_fmt')}, expected h264/yuv420p")
    if (int(video.get("width") or 0), int(video.get("height") or 0)) != (width, height):
        problems.append(f"video is {video.get('width')}x{video.get('height')}, expected {width}x{height}")
    if abs(fps - s.RENDER_FPS) > 0.01:
        problems.append(f"video is {fps:g} fps, expected {s.RENDER_FPS}")
    if audio.get("codec_name") != "aac" or str(audio.get("sample_rate")) != str(s.AUDIO_SAMPLE_RATE):
        problems.append("narration is not AAC at the expected sample rate")
    return problems, facts
