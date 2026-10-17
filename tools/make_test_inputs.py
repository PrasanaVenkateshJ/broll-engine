"""Make the generated audio inputs of the evaluation test pack (testpack/inputs/).

Everything here is rights-clean: our own Kokoro TTS voices (Apache-2.0) reading texts we wrote,
FFmpeg-made silence / tones, and copies of the project owner's own voice recordings.

- my_voice/<name>.m4a.m4a  -> core0N_<lang>_<topic>_myvoice.m4a   (byte-for-byte copies, originals kept)
- tts_sources/<name>.txt   -> <name>.m4a  spoken by the Kokoro voice in VOICEOVERS, AAC 128k
- hard07_silent_audio.wav       20 s of digital silence (FFmpeg anullsrc)
- hard08_music_only_chords.wav  30 s of synthesised chords with a soft beat, no voice (FFmpeg aevalsrc)

Run from the project folder (needs the ML packages: kokoro):
    .venv\\Scripts\\python tools\\make_test_inputs.py            (skips files that already exist)
    .venv\\Scripts\\python tools\\make_test_inputs.py --force    (makes them all again)
"""
import argparse
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.pipeline import media  # noqa: E402

INPUTS = ROOT / "testpack" / "inputs"
MY_VOICE = {  # the owner's recordings -> clear test names
    "en_saving.m4a.m4a": "core01_en_saving_tips_myvoice.m4a",
    "en_bakery.m4a.m4a": "core02_en_bakery_story_myvoice.m4a",
    "ta_screens.m4a.m4a": "core03_ta_screen_time_myvoice.m4a",
}
VOICEOVERS = {  # source text -> our voice name (TTS_VOICE_MAP); every one is labelled "TTS" in the README
    "core11_hi_tts_saving": "hi-IN-female",       # Kokoro hf_alpha, Hindi
    "core12_hi_tts_monsoon": "hi-IN-male",        # Kokoro hm_omega, Hindi
    "hard06_five_minute_voiceover": "en-US-male",  # Kokoro am_michael, English
}
# Three chords (C major, A minor, F major, G major) of three sine notes each, 2 s per chord, with a
# soft 2-per-second pulse: clearly music-like sound, no speech.
CHORDS = ("0.2*(sin(2*PI*261.6*t)+sin(2*PI*329.6*t)+sin(2*PI*392.0*t))*if(lt(mod(t,8),2),1,0)"
          "+0.2*(sin(2*PI*220.0*t)+sin(2*PI*261.6*t)+sin(2*PI*329.6*t))*if(between(mod(t,8),2,4),1,0)"
          "+0.2*(sin(2*PI*174.6*t)+sin(2*PI*220.0*t)+sin(2*PI*261.6*t))*if(between(mod(t,8),4,6),1,0)"
          "+0.2*(sin(2*PI*196.0*t)+sin(2*PI*246.9*t)+sin(2*PI*293.7*t))*if(gte(mod(t,8),6),1,0)")


def _need(path: Path, force: bool) -> bool:
    if path.exists() and not force:
        print(f"exists   {path.name}")
        return False
    return True


def copy_recordings(force: bool) -> None:
    for source, target in MY_VOICE.items():
        out = INPUTS / target
        if (INPUTS / "my_voice" / source).is_file() and _need(out, force):
            shutil.copyfile(INPUTS / "my_voice" / source, out)
            print(f"copied   {out.name}")


def make_voiceovers(force: bool) -> None:
    from app.pipeline.tts import KokoroTTS

    tts = KokoroTTS()
    for stem, voice in VOICEOVERS.items():
        out = INPUTS / f"{stem}.m4a"
        if not _need(out, force):
            continue
        text = (INPUTS / "tts_sources" / f"{stem}.txt").read_text(encoding="utf-8").strip()
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "voice.wav"
            tts.synthesize(text, voice, wav)
            media.ffmpeg("-i", str(wav), "-c:a", "aac", "-b:a", "128k", str(out))
        print(f"spoke    {out.name} ({voice}, {media.duration_of(out):.1f} s)")


def make_synthetic(force: bool) -> None:
    silent = INPUTS / "hard07_silent_audio.wav"
    if _need(silent, force):
        media.ffmpeg("-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", "20", "-c:a", "pcm_s16le", str(silent))
        print(f"made     {silent.name}")
    music = INPUTS / "hard08_music_only_chords.wav"
    if _need(music, force):
        pulse = "0.6+0.4*abs(sin(2*PI*1*t))"
        # The expression is quoted: its commas would otherwise split the filter graph.
        media.ffmpeg("-f", "lavfi", "-i", f"aevalsrc='({CHORDS})*({pulse})':s=48000:d=30", "-ac", "1",
                     "-c:a", "pcm_s16le", str(music))
        print(f"made     {music.name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Make the generated audio inputs of testpack/inputs.")
    parser.add_argument("--force", action="store_true", help="make every file again")
    parser.add_argument("--no-tts", action="store_true", help="skip the Kokoro voiceovers")
    args = parser.parse_args(argv)
    copy_recordings(args.force)
    make_synthetic(args.force)
    if not args.no_tts:
        make_voiceovers(args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
