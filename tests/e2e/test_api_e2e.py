"""End to end through the real API + worker, with ALL mock adapters (no keys, no network).

POST a script -> poll the job -> download every output -> check the videos with ffprobe and
check timeline.json / manifest.json follow the rules in CLAUDE.md.
"""
import json
from fractions import Fraction

from app.pipeline import media
from app.pipeline.providers.mock import MockProvider
from tests.conftest import AUTH
from tests.integration.test_job_flow import path_of, wait_for_job

URL = "/api/v1/broll/jobs"
SCRIPT = ("Saving money is not about earning more. It is about spending wisely. In 2025, household savings grew by "
          "40 percent. Cloud computing now lets small businesses work from anywhere. Trust is earned slowly, one "
          "honest decision at a time.")
SIZES = {"video_16x9.mp4": (1920, 1080), "video_9x16.mp4": (1080, 1920), "video_1x1.mp4": (1080, 1080)}
SCENE_FIELDS = {"index", "text", "start", "end", "query", "flags", "provider", "clip_id", "page_url", "licence",
                "licence_url", "attribution", "commercial_use", "watermark", "ai_generated", "generated", "reason",
                "relevance_score"}


def fetch(client, final, name, tmp_path):
    resp = client.get(path_of(final["result"]["files"][name]))
    assert resp.status_code == 200, name
    path = tmp_path / name
    path.write_bytes(resp.content)
    return path


def run_job(client, body, timeout=180):
    resp = client.post(URL, json=body, headers={**AUTH, "Idempotency-Key": f"e2e-{hash(json.dumps(body))}"})
    assert resp.status_code == 202
    final, _ = wait_for_job(client, resp.json()["job_id"], timeout=timeout)
    assert final["status"] == "succeeded", final["error"]
    return final


def test_full_job_all_ratios_with_mocks(client, worker, tmp_path):
    final = run_job(client, {"script": SCRIPT, "aspect_ratios": ["16:9", "9:16", "1:1"],
                             "avoid": ["people's faces"], "style": "calm"})
    assert set(final["result"]["files"]) == set(SIZES) | {"captions.srt", "timeline.json", "manifest.json"}

    timeline = json.loads(fetch(client, final, "timeline.json", tmp_path).read_text(encoding="utf-8"))
    scenes = timeline["scenes"]
    # Narration sets the timing: contiguous, from 0 to the end of the audio.
    assert scenes[0]["start"] == 0 and scenes[-1]["end"] == timeline["duration"]
    assert all(a["end"] == b["start"] for a, b in zip(scenes, scenes[1:]))
    for sc in scenes:
        assert SCENE_FIELDS <= set(sc), SCENE_FIELDS - set(sc)
        assert sc["commercial_use"] is True and sc["watermark"] is False and sc["ai_generated"] is False
        assert sc["reason"] and sc["licence"]
        assert set(sc["segments"]) == {"16:9", "9:16", "1:1"}
    clip_ids = [(sc["provider"], sc["clip_id"]) for sc in scenes]
    assert len(clip_ids) == len(set(clip_ids))  # no clip twice
    stat = next(sc for sc in scenes if "40 percent" in sc["text"])
    assert stat["flags"]["statistic"] and stat["generated"] and stat["provider"] == "generated"
    assert any(sc["provider"] == "mock" and sc["relevance_score"] >= 0.22 for sc in scenes)
    assert all("portrait" not in q for sc in scenes for q in sc["queries"])  # avoid list honoured

    for name, (w, h) in SIZES.items():
        info = media.probe(fetch(client, final, name, tmp_path))
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
        assert (video["codec_name"], video["pix_fmt"], video["width"], video["height"]) == ("h264", "yuv420p", w, h)
        assert Fraction(video["avg_frame_rate"]) == 30
        assert (audio["codec_name"], audio["sample_rate"]) == ("aac", "48000")
        assert abs(float(video["duration"]) - timeline["duration"]) < 0.1

    manifest = json.loads(fetch(client, final, "manifest.json", tmp_path).read_text(encoding="utf-8"))
    assert manifest["models"]["understand"]["provider"] == "mock"
    assert manifest["models"]["ranker"]["scorer"] == "mock"
    assert manifest["models"]["ffmpeg"].startswith("ffmpeg version")
    assert manifest["providers"]["mock"]["scenes_used"] >= 1 and manifest["providers"]["generated"]["scenes_used"] >= 1
    assert manifest["seed"] == 1234 and manifest["settings"]["STOCK_MIN_SCORE"] == 0.22
    assert {"tts", "transcribe", "scenes", "understand", "footage", "render"} <= set(manifest["timings_seconds"])
    assert manifest["cost_estimate"]["total_usd"] == 0.0 and "free" in manifest["cost_estimate"]["method"]
    assert manifest["rights"]["all_commercial_use"] is True and manifest["rights"]["any_watermark"] is False
    assert manifest["ai_generated"]["visuals"] is False and manifest["ai_generated"]["generated_cards"] >= 1
    assert all(q["problems"] == [] and q["drift_seconds"] < 0.1 for q in manifest["qa"].values())
    assert fetch(client, final, "captions.srt", tmp_path).read_text(encoding="utf-8").startswith("1\n00:00:00")


def test_job_still_succeeds_when_the_footage_provider_is_down(client, worker, tmp_path, monkeypatch):
    monkeypatch.setattr(MockProvider, "fail", True)
    final = run_job(client, {"script": "Small shops open early. Cloud computing helps them grow.",
                             "aspect_ratios": ["1:1"]})
    assert any("Footage source mock failed" in w for w in final["warnings"])
    timeline = json.loads(fetch(client, final, "timeline.json", tmp_path).read_text(encoding="utf-8"))
    assert {sc["provider"] for sc in timeline["scenes"]} == {"generated"}


def test_named_person_job_warns_and_avoids_faces(client, worker, tmp_path):
    final = run_job(client, {"script": "Elon Musk wants rockets to land safely.", "aspect_ratios": ["16:9"]})
    assert any("named person (Elon Musk)" in w for w in final["warnings"])
    timeline = json.loads(fetch(client, final, "timeline.json", tmp_path).read_text(encoding="utf-8"))
    sc = timeline["scenes"][0]
    assert sc["flags"]["named_person"] and sc["safety"]["no_faces"]
    assert all("musk" not in q.lower() for q in sc["queries"])
