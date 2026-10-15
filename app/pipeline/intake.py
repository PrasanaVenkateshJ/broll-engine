"""Step 1 of the pipeline: check and fetch the input, and turn any audio into one clean format.

- Script / scene texts: must contain real text            -> EMPTY_SCRIPT
- audio_url: download with a size and a time limit         -> DOWNLOAD_FAILED, FILE_TOO_LARGE
- Any audio: ffprobe looks INSIDE the file (the extension can lie)
                                                           -> UNSUPPORTED_FILE_TYPE, CORRUPTED_MEDIA
- Longer than MAX_AUDIO_SECONDS                            -> FILE_TOO_LONG
- Then convert to WAV, 48 kHz, mono, so every later step gets the same kind of file.
"""
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.config import get_settings
from app.errors import AppError, ErrorCode
from app.pipeline import media

MB = 1024 * 1024

# What ffprobe calls each allowed type. ffprobe names the m4a/mp4 family "mov,mp4,m4a,3gp,3g2,mj2".
FORMAT_NAMES = {
    "mp3": {"mp3"},
    "wav": {"wav"},
    "m4a": {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"},
}


@dataclass
class AudioProbe:
    kind: str        # real type found inside the file: mp3 | wav | m4a
    codec: str       # e.g. mp3, pcm_s16le, aac
    duration: float  # seconds


@dataclass
class AudioInfo:
    path: Path       # the converted 48 kHz mono WAV
    duration: float
    source_kind: str
    source_codec: str


# ---------- Text checks ----------

def check_script(text: str | None) -> str:
    """Return the cleaned script, or raise EMPTY_SCRIPT if it is empty / only spaces."""
    cleaned = (text or "").strip()
    if not cleaned:
        raise AppError(ErrorCode.EMPTY_SCRIPT, "The script is empty", 422)
    return cleaned


def check_scene_texts(scenes: list[dict]) -> None:
    empty = [i for i, scene in enumerate(scenes) if not (scene.get("text") or "").strip()]
    if empty:
        raise AppError(ErrorCode.EMPTY_SCRIPT, "Some scenes have no text", 422, details={"scene_indexes": empty})


# ---------- Download ----------

def download_audio(url: str, dest: Path) -> Path:
    """Download url to dest, stopping at MAX_DOWNLOAD_MB and DOWNLOAD_TIMEOUT_SECONDS."""
    s = get_settings()
    max_bytes = s.MAX_DOWNLOAD_MB * MB
    deadline = time.monotonic() + s.DOWNLOAD_TIMEOUT_SECONDS
    too_large = AppError(ErrorCode.FILE_TOO_LARGE, f"The audio file is larger than {s.MAX_DOWNLOAD_MB} MB", 413,
                         details={"max_bytes": max_bytes})
    size = 0
    try:
        with httpx.stream("GET", url, timeout=s.DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=True) as resp:
            if resp.status_code != 200:
                raise AppError(ErrorCode.DOWNLOAD_FAILED, f"Download failed with HTTP {resp.status_code}", 422,
                               retryable=resp.status_code >= 500, details={"http_status": resp.status_code})
            declared = resp.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > max_bytes:
                raise too_large  # the server told us the size: stop before downloading anything
            with dest.open("wb") as out:
                for chunk in resp.iter_bytes(MB):
                    size += len(chunk)
                    if size > max_bytes:
                        raise too_large  # no (or wrong) size header: stop as soon as we pass the limit
                    if time.monotonic() > deadline:
                        raise AppError(ErrorCode.DOWNLOAD_FAILED,
                                       f"Download took longer than {s.DOWNLOAD_TIMEOUT_SECONDS:g} s", 422, retryable=True)
                    out.write(chunk)
    except httpx.HTTPError as e:
        dest.unlink(missing_ok=True)
        raise AppError(ErrorCode.DOWNLOAD_FAILED, f"Could not download the audio ({type(e).__name__})", 422,
                       retryable=True) from None
    except AppError:
        dest.unlink(missing_ok=True)
        raise
    if size == 0:
        dest.unlink(missing_ok=True)
        raise AppError(ErrorCode.DOWNLOAD_FAILED, "The downloaded file is empty", 422)
    return dest


# ---------- Real file type ----------

def _looks_like_audio(path: Path) -> bool:
    """Do the first bytes look like mp3 / wav / m4a? Used only to choose the error code."""
    with path.open("rb") as f:
        head = f.read(12)
    return (head.startswith(b"ID3")                                       # mp3 with tags
            or (len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0)  # mp3 frame
            or head.startswith(b"RIFF")                                   # wav
            or head[4:8] == b"ftyp")                                      # m4a / mp4


def check_audio_file(path: Path) -> AudioProbe:
    """Look inside the file with ffprobe. Raises the right error code, or returns what it found."""
    s = get_settings()
    info = media.probe(path)
    if not info or not info.get("format"):
        if _looks_like_audio(path):
            raise AppError(ErrorCode.CORRUPTED_MEDIA, "The audio file is damaged and cannot be read", 422)
        raise AppError(ErrorCode.UNSUPPORTED_FILE_TYPE, "The file is not an audio file", 415,
                       details={"allowed": sorted(s.allowed_audio_extensions)})

    names = set(info["format"].get("format_name", "").split(","))
    kind = next((k for k, fmt in FORMAT_NAMES.items() if names & fmt and k in s.allowed_audio_extensions), None)
    streams = info.get("streams", [])
    audio = [st for st in streams if st.get("codec_type") == "audio"]
    # Cover art inside an mp3/m4a shows up as a "video" stream with attached_pic=1; that is fine.
    video = [st for st in streams if st.get("codec_type") == "video"
             and not st.get("disposition", {}).get("attached_pic")]
    if kind is None or not audio or video:
        raise AppError(ErrorCode.UNSUPPORTED_FILE_TYPE,
                       f"Unsupported file type '{info['format'].get('format_name', 'unknown')}'", 415,
                       details={"allowed": sorted(s.allowed_audio_extensions)})

    try:
        duration = float(info["format"].get("duration") or audio[0].get("duration") or 0)
    except ValueError:
        duration = 0.0
    if duration <= 0:
        raise AppError(ErrorCode.CORRUPTED_MEDIA, "The audio file has no playable length", 422)
    if duration > s.MAX_AUDIO_SECONDS:
        raise AppError(ErrorCode.FILE_TOO_LONG,
                       f"The audio is {duration:.0f} s long; the limit is {s.MAX_AUDIO_SECONDS:.0f} s", 422,
                       details={"duration_seconds": round(duration, 2), "max_seconds": s.MAX_AUDIO_SECONDS})
    return AudioProbe(kind=kind, codec=audio[0].get("codec_name", "unknown"), duration=duration)


# ---------- Convert ----------

def convert_to_wav(src: Path, dest: Path, pad_seconds: float = 0.0) -> Path:
    """Any audio -> 16-bit WAV, AUDIO_SAMPLE_RATE, mono. pad_seconds adds silence at the end."""
    s = get_settings()
    af = ["-af", f"apad=pad_dur={pad_seconds}"] if pad_seconds > 0 else []
    try:
        media.ffmpeg("-i", str(src), "-vn", *af, "-ac", "1", "-ar", str(s.AUDIO_SAMPLE_RATE),
                     "-c:a", "pcm_s16le", str(dest))
    except media.MediaError:
        raise AppError(ErrorCode.CORRUPTED_MEDIA, "The audio could not be decoded", 422) from None
    return dest


def prepare_audio(src: Path, dest: Path) -> AudioInfo:
    """Check the real type, convert to 48 kHz mono WAV, and measure the result."""
    found = check_audio_file(src)
    convert_to_wav(src, dest)
    duration = media.duration_of(dest)
    if duration <= 0:
        raise AppError(ErrorCode.CORRUPTED_MEDIA, "The audio decoded to nothing", 422)
    return AudioInfo(path=dest, duration=duration, source_kind=found.kind, source_codec=found.codec)
