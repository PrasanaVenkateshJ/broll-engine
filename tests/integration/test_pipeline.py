"""The real pipeline through the API + worker, with mock TTS/transcription (no models, no keys)."""
import json
from fractions import Fraction

import pytest

from app.config import get_settings
from app.db import new_session
from app.models import Job
from app.pipeline import media
from tests.conftest import AUTH
from tests.integration.test_job_flow import path_of, wait_for_job
from tests.media_helpers import silence_bytes, tone_bytes
from tests.unit.test_errors import assert_error_format
from tests.unit.test_intake import FileServer

URL = "/api/v1/broll/jobs"
SCRIPT = ("Saving money is not about earning more. It is about spending wisely. In 2025, household savings "
          "grew by 40 percent. Cloud computing now lets small businesses work from anywhere.")


def job_count() -> int:
    with new_session() as db:
        return db.query(Job).count()


def download(client, final, name, tmp_path):
    resp = client.get(path_of(final["result"]["files"][name]))
    assert resp.status_code == 200
    path = tmp_path / name
    path.write_bytes(resp.content)
    return path


def test_script_job_makes_correct_videos_srt_and_timeline(client, worker, tmp_path):
    resp = client.post(URL, json={"script": SCRIPT, "aspect_ratios": ["16:9", "9:16"]}, headers=AUTH)
    final, _ = wait_for_job(client, resp.json()["job_id"])
    assert final["status"] == "succeeded", final["error"]
    assert set(final["result"]["files"]) == {"video_16x9.mp4", "video_9x16.mp4", "captions.srt",
                                              "timeline.json", "manifest.json"}

    timeline = json.loads(download(client, final, "timeline.json", tmp_path).read_text(encoding="utf-8"))
    scenes = timeline["scenes"]
    assert scenes[0]["start"] == 0 and scenes[-1]["end"] == timeline["duration"]
    assert all(a["end"] == b["start"] for a, b in zip(scenes, scenes[1:]))

    for name, (w, h) in {"video_16x9.mp4": (1920, 1080), "video_9x16.mp4": (1080, 1920)}.items():
        info = media.probe(download(client, final, name, tmp_path))
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
        assert (video["codec_name"], video["pix_fmt"], video["width"], video["height"]) == ("h264", "yuv420p", w, h)
        assert Fraction(video["avg_frame_rate"]) == 30
        assert (audio["codec_name"], audio["sample_rate"]) == ("aac", "48000")
        assert abs(float(video["duration"]) - timeline["duration"]) < 0.1  # video matches the narration

    srt = download(client, final, "captions.srt", tmp_path).read_text(encoding="utf-8")
    assert srt.startswith("1\n00:00:")
    manifest = json.loads(download(client, final, "manifest.json", tmp_path).read_text(encoding="utf-8"))
    assert manifest["models"]["tts"]["provider"] == "mock"
    assert manifest["ai_generated"]["narration"] is True


def test_scene_list_job_one_scene_per_item_with_hints(client, worker, tmp_path):
    items = [{"text": "Money grows when you save a little every week.", "visual_hint": "coins in a glass jar"},
             {"text": "Small habits add up.", "visual_hint": None},
             {"text": "Start today, not tomorrow, and keep going."}]
    resp = client.post(URL, json={"scenes": items, "aspect_ratios": ["1:1"], "captions": False}, headers=AUTH)
    final, _ = wait_for_job(client, resp.json()["job_id"])
    assert final["status"] == "succeeded", final["error"]
    scenes = json.loads(download(client, final, "timeline.json", tmp_path).read_text(encoding="utf-8"))["scenes"]
    assert [s["text"] for s in scenes] == [i["text"] for i in items]
    assert [s["visual_hint"] for s in scenes] == ["coins in a glass jar", None, None]
    assert all(a["end"] == b["start"] for a, b in zip(scenes, scenes[1:]))


def test_audio_url_job_downloads_and_succeeds(client, worker):
    server = FileServer({"/voice.mp3": (200, tone_bytes(".mp3", 3.0), True, 0)})
    try:
        resp = client.post(URL, json={"audio_url": f"{server.base}/voice.mp3", "aspect_ratios": ["1:1"]},
                           headers=AUTH)
        final, _ = wait_for_job(client, resp.json()["job_id"])
    finally:
        server.close()
    assert final["status"] == "succeeded", final["error"]


def test_audio_url_that_fails_to_download_fails_the_job(client, worker):
    resp = client.post(URL, json={"audio_url": "http://127.0.0.1:9/voice.mp3", "aspect_ratios": ["1:1"]},
                       headers=AUTH)
    assert resp.status_code == 202  # cannot know before trying, so it becomes a job
    final, _ = wait_for_job(client, resp.json()["job_id"])
    assert final["status"] == "failed"
    assert final["error"]["code"] == "DOWNLOAD_FAILED"


def test_silent_upload_fails_with_no_speech_detected(client, worker):
    resp = client.post(URL, files={"audio": ("quiet.wav", silence_bytes(".wav"), "audio/wav")}, headers=AUTH)
    assert resp.status_code == 202
    final, _ = wait_for_job(client, resp.json()["job_id"])
    assert final["status"] == "failed"
    assert final["error"]["code"] == "NO_SPEECH_DETECTED"


# ---------- Errors found BEFORE queuing: 4xx at once, no job created ----------

def test_text_renamed_to_mp3_is_rejected_immediately(client):
    resp = client.post(URL, files={"audio": ("song.mp3", b"just some text, not audio\n" * 50, "audio/mpeg")},
                       headers=AUTH)
    assert resp.status_code == 415
    assert_error_format(resp, "UNSUPPORTED_FILE_TYPE")
    assert job_count() == 0


def test_too_long_upload_is_rejected_immediately(client, monkeypatch):
    monkeypatch.setenv("MAX_AUDIO_SECONDS", "2")
    get_settings.cache_clear()
    resp = client.post(URL, files={"audio": ("long.m4a", tone_bytes(".m4a", 4.0), "audio/mp4")}, headers=AUTH)
    assert resp.status_code == 422
    assert_error_format(resp, "FILE_TOO_LONG")
    assert job_count() == 0


@pytest.mark.parametrize("body", [{"script": "   \n  "}, {"scenes": [{"text": "Fine."}, {"text": "  "}]}])
def test_empty_script_is_rejected_immediately(client, body):
    resp = client.post(URL, json=body, headers=AUTH)
    assert resp.status_code == 422
    assert_error_format(resp, "EMPTY_SCRIPT")
    assert job_count() == 0


def test_unknown_voice_is_rejected_immediately(client):
    resp = client.post(URL, json={"script": "Hello.", "voice": "pirate-parrot"}, headers=AUTH)
    assert resp.status_code == 422
    assert_error_format(resp, "INVALID_INPUT")
