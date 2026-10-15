"""Small helpers to call ffmpeg and ffprobe (separate programs) with subprocess.

Every pipeline step that touches audio or video goes through here, so timeouts and
error handling are the same everywhere.
"""
import json
import subprocess
from pathlib import Path

from app.config import get_settings


class MediaError(Exception):
    """ffmpeg/ffprobe failed. `output` holds the end of what the program printed."""

    def __init__(self, message: str, output: str = ""):
        super().__init__(message)
        self.output = output


def run(args: list[str], cwd: str | Path | None = None, timeout: float | None = None) -> subprocess.CompletedProcess:
    """Run a program and capture its output as text. Raises MediaError on failure or timeout."""
    s = get_settings()
    try:
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=timeout or s.FFMPEG_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        raise MediaError(f"{Path(args[0]).name} timed out") from e
    except FileNotFoundError as e:
        raise MediaError(f"{args[0]} is not installed or not on PATH") from e
    if result.returncode != 0:
        raise MediaError(f"{Path(args[0]).name} failed (exit {result.returncode})", result.stderr[-2000:])
    return result


def ffmpeg(*args: str, cwd: str | Path | None = None, loglevel: str = "error") -> subprocess.CompletedProcess:
    """ffmpeg with the options we always want: no banner, never ask questions, overwrite outputs."""
    s = get_settings()
    return run([s.FFMPEG_BIN, "-hide_banner", "-nostdin", "-y", "-loglevel", loglevel, *args], cwd=cwd)


def probe(path: str | Path) -> dict | None:
    """ffprobe the file. Returns {"format": {...}, "streams": [...]} or None if ffprobe cannot read it."""
    s = get_settings()
    try:
        result = run([s.FFPROBE_BIN, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
                     timeout=60)
    except MediaError:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def duration_of(path: str | Path) -> float:
    """Length in seconds (0.0 if unknown)."""
    info = probe(path) or {}
    try:
        return float(info.get("format", {}).get("duration") or 0.0)
    except ValueError:
        return 0.0


def ffmpeg_bytes(*args: str) -> bytes:
    """ffmpeg that writes raw data to stdout (e.g. small grey frames); returns those bytes."""
    s = get_settings()
    cmd = [s.FFMPEG_BIN, "-hide_banner", "-nostdin", "-loglevel", "error", *args]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=s.FFMPEG_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        raise MediaError("ffmpeg timed out") from e
    except FileNotFoundError as e:
        raise MediaError(f"{cmd[0]} is not installed or not on PATH") from e
    if result.returncode != 0:
        raise MediaError(f"ffmpeg failed (exit {result.returncode})", result.stderr.decode(errors="replace")[-2000:])
    return result.stdout


def ffmpeg_version() -> str:
    """First line of `ffmpeg -version`, e.g. "ffmpeg version 7.1.1-1 Copyright ..." (for the manifest)."""
    try:
        first = run([get_settings().FFMPEG_BIN, "-hide_banner", "-version"], timeout=30).stdout.splitlines()[0]
    except (MediaError, IndexError):
        return "unknown"
    return first.split(" Copyright")[0].strip()
