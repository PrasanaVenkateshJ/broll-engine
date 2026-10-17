"""A Hindi job through the API + worker (mock TTS / transcription): English search, Hindi captions."""
import json

from app.pipeline.transcribe import MockTranscriber
from tests.conftest import AUTH
from tests.integration.test_job_flow import wait_for_job
from tests.integration.test_pipeline import download

HINDI = "पैसे बचाना बहुत ज़रूरी है। हर महीने थोड़ा पैसा अलग रखिए।"
ENGLISH = [("Saving money is very important.", 0.0, 2.6), ("Put a little money aside every month.", 2.6, 6.0)]


def test_hindi_script_searches_in_english_and_keeps_hindi_captions(client, worker, tmp_path, monkeypatch):
    asked = []

    def fake_translate(self, audio_path, language):
        asked.append(language)
        return ENGLISH

    monkeypatch.setattr(MockTranscriber, "translate", fake_translate)
    resp = client.post("/api/v1/broll/jobs", json={"script": HINDI, "voice": "hi-IN-female",
                                                   "aspect_ratios": ["16:9"]}, headers=AUTH)
    final, _ = wait_for_job(client, resp.json()["job_id"])
    assert final["status"] == "succeeded", final["error"]
    assert asked == ["hi"]  # the voice told us the language, and only non-English speech is translated

    timeline = json.loads(download(client, final, "timeline.json", tmp_path).read_text(encoding="utf-8"))
    assert timeline["language"] == "hi"
    for sc in timeline["scenes"]:
        assert sc["search_text"] and sc["search_text"].isascii()
        assert all(q.isascii() for q in sc["queries"]), sc["queries"]
        assert not sc["text"].isascii()  # the scene text stays as spoken
        assert sc["segments"]["16:9"]["caption_font"] == "Noto Sans Devanagari"
    srt = download(client, final, "captions.srt", tmp_path).read_text(encoding="utf-8")
    assert "बचाना" in srt and "Saving" not in srt
    manifest = json.loads(download(client, final, "manifest.json", tmp_path).read_text(encoding="utf-8"))
    translated = manifest["models"]["transcribe"]["translate_for_search"]
    assert translated["from_language"] == "hi" and translated["scenes_with_english"] == len(timeline["scenes"])


def test_english_job_is_not_translated(client, worker, monkeypatch):
    monkeypatch.setattr(MockTranscriber, "translate", lambda *a: (_ for _ in ()).throw(AssertionError("called")))
    resp = client.post("/api/v1/broll/jobs", json={"script": "Save a little money every month.",
                                                   "aspect_ratios": ["16:9"]}, headers=AUTH)
    final, _ = wait_for_job(client, resp.json()["job_id"])
    assert final["status"] == "succeeded", final["error"]
