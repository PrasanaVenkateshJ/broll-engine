"""Text -> voice. Adapter pattern: every TTS engine has the same `synthesize` method,
and get_tts() picks one by the TTS_PROVIDER setting.

- KokoroTTS: Kokoro-82M (Apache-2.0), runs on the CPU, downloads the model once.
- MockTTS:   no model. Makes a plain tone as long as the text would take to say,
             so tests run fast, offline, with no keys.
synthesize_cached(): the same text + voice + model + speed is spoken only once (TTS_CACHE_DIR).
"""
import hashlib
import json
import shutil
import threading
import wave
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from app.config import get_settings
from app.errors import AppError, ErrorCode
from app.pipeline import media


@dataclass
class TTSResult:
    path: Path
    voice: str


def check_voice(voice: str) -> tuple[str, str]:
    """Our voice name (e.g. en-IN-female) -> (kokoro language code, kokoro voice id)."""
    voices = get_settings().tts_voices
    if voice not in voices:
        raise AppError(ErrorCode.INVALID_INPUT, f"Unknown voice '{voice}'", 422,
                       details={"allowed_voices": sorted(voices)})
    return voices[voice]


def _package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "not installed"


class TTSAdapter:
    name = "base"

    def synthesize(self, text: str, voice: str, out_path: Path) -> TTSResult:
        """Write the spoken text to out_path (a WAV file)."""
        raise NotImplementedError

    def info(self) -> dict:
        """Model + version, recorded in manifest.json."""
        raise NotImplementedError


class MockTTS(TTSAdapter):
    name = "mock"

    def synthesize(self, text, voice, out_path):
        s = get_settings()
        check_voice(voice)
        seconds = round(len(text.split()) / s.MOCK_WORDS_PER_SECOND + 0.5, 2)  # "correct-ish" length
        media.ffmpeg("-f", "lavfi", "-i", f"sine=frequency=220:sample_rate={s.AUDIO_SAMPLE_RATE}:duration={seconds}",
                     "-ac", "1", "-c:a", "pcm_s16le", str(out_path))
        return TTSResult(path=out_path, voice=voice)

    def info(self):
        return {"provider": "mock", "model": "sine-tone", "version": "1"}


class KokoroTTS(TTSAdapter):
    name = "kokoro"
    SAMPLE_RATE = 24000  # Kokoro always outputs 24 kHz; intake converts to 48 kHz later

    def __init__(self):
        self._model = None
        self._pipelines: dict[str, object] = {}  # one per language code, all sharing one model
        self._lock = threading.Lock()  # worker threads take turns (one CPU model)

    def _pipeline(self, lang_code: str):
        from kokoro import KModel, KPipeline  # imported here: tests with the mock never need torch

        repo = get_settings().KOKORO_REPO_ID
        if self._model is None:
            self._model = KModel(repo_id=repo).eval()
        if lang_code not in self._pipelines:
            self._pipelines[lang_code] = KPipeline(lang_code=lang_code, repo_id=repo, model=self._model)
        return self._pipelines[lang_code]

    def synthesize(self, text, voice, out_path):
        import numpy as np

        lang_code, voice_id = check_voice(voice)
        with self._lock:
            try:
                pipeline = self._pipeline(lang_code)
                # Kokoro splits long text into chunks itself and yields audio for each one.
                chunks = [r.audio.numpy() for r in pipeline(text, voice=voice_id, speed=get_settings().KOKORO_SPEED)
                          if r.audio is not None]
            except Exception as e:
                raise AppError(ErrorCode.PROVIDER_UNAVAILABLE, f"Kokoro TTS failed ({type(e).__name__})", 503,
                               retryable=True) from e
        if not chunks:
            raise AppError(ErrorCode.EMPTY_SCRIPT, "The text produced no speech", 422)
        audio = np.concatenate(chunks)
        pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2")  # float -1..1 -> 16-bit numbers
        with wave.open(str(out_path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.SAMPLE_RATE)
            w.writeframes(pcm.tobytes())
        return TTSResult(path=out_path, voice=voice)

    def info(self):
        return {"provider": "kokoro", "model": get_settings().KOKORO_REPO_ID, "version": _package_version("kokoro"),
                "licence": "Apache-2.0"}


def cache_key(adapter: TTSAdapter, text: str, voice: str) -> str:
    """Same key = the same audio: text, voice, engine + model + version and speed."""
    data = json.dumps({"text": text, "voice": voice, "engine": adapter.info(), "speed": get_settings().KOKORO_SPEED},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(data.encode("utf-8")).hexdigest()[:20]


def synthesize_cached(adapter: TTSAdapter, text: str, voice: str, out_path: Path) -> tuple[TTSResult, bool]:
    """Like adapter.synthesize, but a finished WAV for the same text + voice + model + speed is reused from
    TTS_CACHE_DIR (empty = no cache). Returns (result, True if it came from the cache)."""
    folder = get_settings().TTS_CACHE_DIR
    if not folder:
        return adapter.synthesize(text, voice, out_path), False
    cached = Path(folder) / f"{cache_key(adapter, text, voice)}.wav"
    if cached.is_file() and cached.stat().st_size > 44:  # more than a bare WAV header
        shutil.copyfile(cached, out_path)
        return TTSResult(path=out_path, voice=voice), True
    result = adapter.synthesize(text, voice, out_path)
    try:
        cached.parent.mkdir(parents=True, exist_ok=True)
        tmp = cached.with_suffix(f".{threading.get_ident()}.tmp")
        shutil.copyfile(result.path, tmp)
        tmp.replace(cached)  # atomic: another job never reads a half-written file
    except OSError:
        pass  # a cache problem only means "no reuse next time"
    return result, False


_adapters: dict[str, TTSAdapter] = {}
_adapters_lock = threading.Lock()


def get_tts() -> TTSAdapter:
    """The adapter chosen by TTS_PROVIDER. Made once and reused (Kokoro loads a model)."""
    name = get_settings().TTS_PROVIDER
    with _adapters_lock:
        if name not in _adapters:
            _adapters[name] = KokoroTTS() if name == "kokoro" else MockTTS()
        return _adapters[name]
