"""QA checks: black frames, frozen picture, frame count, duration drift and codecs."""
import subprocess

from app.pipeline import qa
from tests.media_helpers import make_tone, make_video


def test_good_clip_passes(tmp_path):
    assert qa.check_segment(make_video(tmp_path / "ok.mp4", 3.0), 90) == []


def test_black_video_is_found(tmp_path):
    problems = qa.check_segment(make_video(tmp_path / "black.mp4", 3.0, source="color=c=black"), 90)
    assert any(p.startswith("black frames") for p in problems)


def test_frozen_video_is_found(tmp_path):
    problems = qa.check_segment(make_video(tmp_path / "still.mp4", 3.0, source="color=c=steelblue"), 90)
    assert any(p.startswith("frozen picture") for p in problems)


def test_wrong_frame_count_is_found(tmp_path):
    assert "90 frames instead of 120" in qa.check_segment(make_video(tmp_path / "v.mp4", 3.0), 120)


def test_final_video_drift_and_codec_checks(tmp_path):
    video = make_video(tmp_path / "v.mp4", 5.0, size="1920x1080")
    audio = make_tone(tmp_path / "a.wav", 5.0)
    out = tmp_path / "out.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video), "-i", str(audio),
                    "-c:v", "copy", "-c:a", "aac", "-ar", "48000", str(out)], check=True)
    problems, facts = qa.check_video(out, 5.0, 1920, 1080)
    assert problems == [] and facts["drift_seconds"] < 0.1
    problems, _ = qa.check_video(out, 5.5, 1920, 1080)  # narration 0.5 s longer than the video
    assert problems == ["video and narration lengths differ by 0.500 s"]
