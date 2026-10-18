"""Download every AI model at BUILD time, so a cold start of the live demo downloads nothing.

It loads each model exactly the way the app does (same classes, same settings), so the files land in
the same cache folders (HF_HOME, TORCH_HOME) that the running app will read:
- Whisper (WHISPER_MODEL, default "small") via the app's WhisperTranscriber,
- Kokoro (KOKORO_REPO_ID) + one short sentence in EVERY voice of TTS_VOICE_MAP (each voice is its
  own file; speaking once also checks the pronunciation step works),
- open_clip (CLIP_MODEL + CLIP_PRETRAINED, default ViT-B-32) via the app's ClipScorer,
- spaCy en_core_web_sm (installed by requirements-ml.txt; loaded here to prove it is there).
Any failure stops the Docker build with an error, so a broken image is never deployed.

Run (inside the Modal image build, offline): python deploy/modal/preload_models.py
"""
import tempfile
import time
from pathlib import Path

from app.config import get_settings
from app.pipeline.rank import ClipScorer
from app.pipeline.transcribe import WhisperTranscriber
from app.pipeline.tts import KokoroTTS


def step(name, fn):
    start = time.perf_counter()
    fn()
    print(f"preload: {name} ok ({time.perf_counter() - start:.1f} s)", flush=True)


def kokoro_all_voices():
    tts = KokoroTTS()
    with tempfile.TemporaryDirectory() as tmp:
        for voice in get_settings().tts_voices:
            tts.synthesize("Hello.", voice, Path(tmp) / f"{voice}.wav")
            print(f"preload:   voice {voice} ok", flush=True)


def spacy_model():
    import spacy

    spacy.load("en_core_web_sm")


if __name__ == "__main__":
    s = get_settings()
    step(f"whisper {s.WHISPER_MODEL}", lambda: WhisperTranscriber()._load())
    step(f"kokoro {s.KOKORO_REPO_ID}", kokoro_all_voices)
    step(f"open_clip {s.CLIP_MODEL} / {s.CLIP_PRETRAINED}", ClipScorer._load)
    step("spacy en_core_web_sm", spacy_model)
