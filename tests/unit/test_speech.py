"""TTS mock + voice names, mock transcriber, and the silent / music-only checks."""
import pytest

from app.errors import AppError
from app.pipeline import media
from app.pipeline.transcribe import (MockTranscriber, SoundCheck, Transcript, Word, check_sound, check_speech,
                                     get_transcriber)
from app.pipeline.tts import MockTTS, check_voice, get_tts
from tests.media_helpers import make_silence, make_tone


def test_providers_are_chosen_by_settings():
    assert get_tts().name == "mock"
    assert get_transcriber().name == "mock"


def test_voice_names_map_to_kokoro_voices():
    assert check_voice("en-US-female") == ("a", "af_heart")
    assert check_voice("hi-IN-female") == ("h", "hf_alpha")
    with pytest.raises(AppError) as e:
        check_voice("klingon-robot")
    assert e.value.code.value == "INVALID_INPUT"
    assert "en-IN-female" in e.value.details["allowed_voices"]


def test_mock_tts_length_follows_the_text(tmp_path):
    short = MockTTS().synthesize("Hello there.", "en-US-female", tmp_path / "a.wav").path
    long = MockTTS().synthesize(" ".join(["word"] * 25), "en-US-female", tmp_path / "b.wav").path
    assert abs(media.duration_of(short) - (2 / 2.5 + 0.5)) < 0.05  # 2 words at 2.5 words/s + 0.5 s
    assert abs(media.duration_of(long) - (25 / 2.5 + 0.5)) < 0.05
    assert MockTTS().info()["provider"] == "mock"


def test_mock_transcriber_spreads_hints_in_order():
    t = MockTranscriber().transcribe(None, 6.0, hints=[("One two three.", 0.0, 3.0), ("Four five.", 3.0, 6.0)])
    assert [w.text for w in t.words] == ["One", "two", "three.", "Four", "five."]
    assert all(a.end <= b.start for a, b in zip(t.words, t.words[1:]))
    assert 0 <= t.words[0].start and t.words[-1].end <= 6.0
    assert t.words[3].start >= 3.0  # second hint's words stay in its own time span


def test_silent_audio_is_no_speech_detected(tmp_path):
    silent = make_silence(tmp_path / "silent.wav", seconds=3.0)
    with pytest.raises(AppError) as e:
        check_sound(silent, 3.0)
    assert e.value.code.value == "NO_SPEECH_DETECTED"


def test_audio_with_sound_passes_the_sound_check(tmp_path):
    tone = make_tone(tmp_path / "tone.wav", seconds=3.0)
    result = check_sound(tone, 3.0)
    assert result.max_volume_db > -45
    assert result.sound_seconds > 2.5


def _transcript(words, no_speech=0.05):
    return Transcript(words=words, language="en", language_probability=0.99, no_speech_prob=no_speech)


def _words(n, prob=0.9):
    return [Word(f"word{i}", i * 0.4, i * 0.4 + 0.3, prob) for i in range(n)]


def test_normal_speech_passes_the_music_check():
    check_speech(_transcript(_words(25)), SoundCheck(max_volume_db=-3, sound_seconds=10))


@pytest.mark.parametrize("transcript", [
    _transcript([]),                                     # loud sound, no words at all
    _transcript(_words(2)),                              # 2 words in 60 s of sound
    _transcript([Word("♪", 0, 1), Word("♪♪", 1, 2)]),    # Whisper only "heard" music notes
    _transcript(_words(30), no_speech=0.9),              # Whisper itself says: not speech
    _transcript(_words(30, prob=0.1)),                   # words, but all guesses
])
def test_sound_without_real_speech_is_music_only(transcript):
    with pytest.raises(AppError) as e:
        check_speech(transcript, SoundCheck(max_volume_db=-3, sound_seconds=60))
    assert e.value.code.value == "MUSIC_ONLY_AUDIO"
    assert "words_per_minute" in e.value.details
