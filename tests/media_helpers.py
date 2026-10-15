"""Make tiny test audio with ffmpeg (sine tones and silence), so no real recordings are needed."""
import subprocess
from functools import lru_cache
from pathlib import Path

CODECS = {".wav": ["-c:a", "pcm_s16le"], ".mp3": ["-c:a", "libmp3lame", "-b:a", "64k"],
          ".m4a": ["-c:a", "aac", "-b:a", "64k"], ".flac": ["-c:a", "flac"]}


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error", *args], check=True)


def make_tone(path: Path, seconds: float = 2.0, frequency: int = 440) -> Path:
    """A plain beep. Suffix (.wav/.mp3/.m4a/.flac) picks the format."""
    _ffmpeg("-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=44100:duration={seconds}",
            *CODECS[path.suffix], str(path))
    return path


def make_silence(path: Path, seconds: float = 3.0) -> Path:
    _ffmpeg("-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", str(seconds), *CODECS[path.suffix], str(path))
    return path


@lru_cache
def tone_bytes(suffix: str = ".wav", seconds: float = 2.0) -> bytes:
    """Same as make_tone, but returns the bytes (made once per test run, then reused)."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        return make_tone(Path(tmp) / f"tone{suffix}", seconds).read_bytes()


@lru_cache
def silence_bytes(suffix: str = ".wav", seconds: float = 3.0) -> bytes:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        return make_silence(Path(tmp) / f"silence{suffix}", seconds).read_bytes()
