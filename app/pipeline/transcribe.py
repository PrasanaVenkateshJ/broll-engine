"""Voice -> words with start/end times. Same adapter pattern as tts.py.

- WhisperTranscriber: faster-whisper (MIT), word timestamps, language auto-detect, CPU int8.
- MockTranscriber:    no model. Spreads known text evenly over the audio (tests only).

Plus the two speech checks:
- check_sound():  ffmpeg volumedetect + silencedetect. Silent file -> NO_SPEECH_DETECTED.
- check_speech(): there IS sound, but Whisper found almost no words, or says "this is
                  probably not speech" -> MUSIC_ONLY_AUDIO.
"""
import re
import threading
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from app.config import get_settings
from app.errors import AppError, ErrorCode
from app.pipeline import media


@dataclass
class Word:
    text: str    # as spoken, with its punctuation, e.g. "more."
    start: float
    end: float
    probability: float = 1.0


@dataclass
class Transcript:
    words: list[Word]
    language: str
    language_probability: float
    no_speech_prob: float  # 0 = surely speech, 1 = surely not speech
    info: dict = field(default_factory=dict)  # provider/model/version for the manifest

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


@dataclass
class SoundCheck:
    max_volume_db: float
    sound_seconds: float  # how much of the file is NOT silence


# Hints = text we already know, with the time span it covers: [(text, start, end), ...].
# Only the mock uses them; Whisper always listens to the audio itself.
Hints = list[tuple[str, float, float]]


def _package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "not installed"


class Transcriber:
    name = "base"

    def transcribe(self, audio_path: Path, duration: float, hints: Hints | None = None) -> Transcript:
        raise NotImplementedError


class MockTranscriber(Transcriber):
    name = "mock"

    def transcribe(self, audio_path, duration, hints=None):
        hints = hints or [(get_settings().MOCK_TRANSCRIPT, 0.0, duration)]
        words: list[Word] = []
        for text, start, end in hints:
            tokens = text.split()
            if not tokens:
                continue
            # Leave a little room at both ends; longer words get more time.
            begin, finish = start + 0.1, max(start + 0.2, end - 0.2)
            weights = [len(t) + 1 for t in tokens]
            per_unit = (finish - begin) / sum(weights)
            t = begin
            for token, weight in zip(tokens, weights):
                length = weight * per_unit
                words.append(Word(token, round(t, 3), round(t + length * 0.85, 3)))  # small gap between words
                t += length
        return Transcript(words=words, language="en", language_probability=1.0, no_speech_prob=0.0,
                          info={"provider": "mock", "model": "even-spread", "version": "1"})


class WhisperTranscriber(Transcriber):
    name = "whisper"

    def __init__(self):
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel  # imported here: mock tests never need it

            s = get_settings()
            self._model = WhisperModel(s.WHISPER_MODEL, device=s.WHISPER_DEVICE, compute_type=s.WHISPER_COMPUTE_TYPE)
        return self._model

    def transcribe(self, audio_path, duration, hints=None):
        s = get_settings()
        with self._lock:
            try:
                model = self._load()
                segments, info = model.transcribe(
                    str(audio_path), word_timestamps=True, language=s.WHISPER_LANGUAGE or None,
                    beam_size=s.WHISPER_BEAM_SIZE, vad_filter=s.WHISPER_VAD_FILTER)
                segments = list(segments)  # the real work happens while we read the segments
            except Exception as e:
                raise AppError(ErrorCode.PROVIDER_UNAVAILABLE, f"Transcription failed ({type(e).__name__})", 503,
                               retryable=True) from e

        words: list[Word] = []
        last_end = 0.0
        for seg in segments:
            for w in seg.words or []:
                text = w.word.strip()
                if not text:
                    continue
                start = min(max(w.start, last_end), duration)  # keep times in order and inside the audio
                end = min(max(w.end, start), duration)
                words.append(Word(text, round(start, 3), round(end, 3), round(w.probability, 3)))
                last_end = end

        # Average "no speech" probability, weighted by how long each segment is.
        total = sum(max(seg.end - seg.start, 0.01) for seg in segments)
        no_speech = (sum(seg.no_speech_prob * max(seg.end - seg.start, 0.01) for seg in segments) / total
                     if segments else 1.0)
        return Transcript(words=words, language=info.language, language_probability=round(info.language_probability, 3),
                          no_speech_prob=round(no_speech, 3),
                          info={"provider": "faster-whisper", "model": s.WHISPER_MODEL,
                                "version": _package_version("faster-whisper"), "compute_type": s.WHISPER_COMPUTE_TYPE,
                                "licence": "MIT"})


_adapters: dict[str, Transcriber] = {}
_adapters_lock = threading.Lock()


def get_transcriber() -> Transcriber:
    name = get_settings().TRANSCRIBE_PROVIDER
    with _adapters_lock:
        if name not in _adapters:
            _adapters[name] = WhisperTranscriber() if name == "whisper" else MockTranscriber()
        return _adapters[name]


# ---------- Speech checks ----------

_NUMBER = r"(-?inf|-?\d+(?:\.\d+)?)"


def check_sound(audio_path: Path, duration: float) -> SoundCheck:
    """Measure loudness and silence with ffmpeg. Raises NO_SPEECH_DETECTED if there is no sound."""
    s = get_settings()
    out = media.ffmpeg("-i", str(audio_path), "-af",
                       f"silencedetect=noise={s.SILENCE_NOISE_DB}dB:d={s.SILENCE_MIN_SECONDS},volumedetect",
                       "-f", "null", "-", loglevel="info").stderr
    max_match = re.search(rf"max_volume:\s*{_NUMBER}\s*dB", out)
    max_db = float(max_match.group(1)) if max_match else float("-inf")

    starts = [float(x) for x in re.findall(rf"silence_start:\s*{_NUMBER}", out)]
    ends = [float(x) for x in re.findall(rf"silence_end:\s*{_NUMBER}", out)]
    ends += [duration] * (len(starts) - len(ends))  # silence that runs to the end of the file
    silent = sum(max(0.0, min(e, duration) - max(st, 0.0)) for st, e in zip(starts, ends))
    result = SoundCheck(max_volume_db=max_db, sound_seconds=round(max(0.0, duration - silent), 3))

    if max_db <= s.SILENCE_MAX_VOLUME_DB or result.sound_seconds < s.MIN_SOUND_SECONDS:
        raise AppError(ErrorCode.NO_SPEECH_DETECTED, "The audio is silent: no speech was found", 422,
                       details={"max_volume_db": max_db, "sound_seconds": result.sound_seconds})
    return result


def check_speech(transcript: Transcript, sound: SoundCheck) -> None:
    """There is sound. Is it speech? Raises MUSIC_ONLY_AUDIO if it does not look like it."""
    s = get_settings()
    words = [w for w in transcript.words if any(c.isalnum() for c in w.text)]  # drop "♪" and lone punctuation
    words_per_minute = len(words) / max(sound.sound_seconds, 1.0) * 60
    mean_prob = sum(w.probability for w in words) / len(words) if words else 0.0
    details = {"words": len(words), "words_per_minute": round(words_per_minute, 1),
               "no_speech_prob": transcript.no_speech_prob, "mean_word_prob": round(mean_prob, 3),
               "sound_seconds": sound.sound_seconds}
    if (not words or words_per_minute < s.MUSIC_MIN_WORDS_PER_MINUTE
            or transcript.no_speech_prob > s.MUSIC_MAX_NO_SPEECH_PROB or mean_prob < s.MUSIC_MIN_WORD_PROB):
        raise AppError(ErrorCode.MUSIC_ONLY_AUDIO, "The audio has sound but almost no speech (music only?)", 422,
                       details=details)
