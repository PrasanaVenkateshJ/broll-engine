"""tools/batch_run.py against the real API + worker (TestClient is an httpx.Client, so the batch
runner talks to it exactly like to a live server). Mock adapters, no keys."""
import csv
import json

from tests.conftest import AUTH
from tests.media_helpers import tone_bytes
from tools.batch_run import collect_inputs, main, run_batch


def make_inputs(folder):
    folder.mkdir()
    (folder / "a_script.txt").write_text("Small shops open early. Cloud computing helps them grow.", encoding="utf-8")
    (folder / "b_scenes.json").write_text(json.dumps({"name": "scene list", "scenes": [
        {"text": "Money grows slowly.", "visual_hint": "coins in a glass jar"}, {"text": "Trust takes time."}]}),
        encoding="utf-8")
    (folder / "c_voice.wav").write_bytes(tone_bytes(".wav", 3.0))
    (folder / "d_empty.txt").write_text("   ", encoding="utf-8")
    (folder / "notes.md").write_text("not an input", encoding="utf-8")
    return folder


def test_collect_inputs_from_a_folder_and_a_jsonl_file(tmp_path):
    items = collect_inputs(make_inputs(tmp_path / "in"))
    assert [i.name for i in items] == ["a_script", "scene_list", "c_voice", "d_empty"]  # notes.md skipped
    assert items[1].body == {"scenes": [{"text": "Money grows slowly.", "visual_hint": "coins in a glass jar"},
                                        {"text": "Trust takes time."}]}  # "name" is not sent to the API
    jsonl = tmp_path / "list.jsonl"
    jsonl.write_text('{"script": "One."}\n\n{"script": "Two.", "name": "two"}\n{"script": "Three."}\n',
                     encoding="utf-8")
    assert [i.name for i in collect_inputs(jsonl)] == ["list-001", "two", "list-004"]


def test_batch_run_downloads_everything_and_writes_the_summary(client, worker, tmp_path):
    client.headers.update(AUTH)
    items = collect_inputs(make_inputs(tmp_path / "in"))
    out = tmp_path / "out"
    results = run_batch(items, out, client, ratios=["1:1"], concurrency=2, timeout=180, poll=0.05)

    by_name = {r.input: r for r in results}
    assert {n: r.status for n, r in by_name.items()} == {"a_script": "succeeded", "scene_list": "succeeded",
                                                         "c_voice": "succeeded", "d_empty": "rejected"}
    assert by_name["d_empty"].error_code == "EMPTY_SCRIPT"
    for name in ("a_script", "scene_list", "c_voice"):
        files = {p.name for p in (out / name).iterdir()}
        assert {"video_1x1.mp4", "captions.srt", "timeline.json", "manifest.json", "job.json"} <= files
        assert "video_16x9.mp4" not in files  # --ratios 1:1 was applied
        assert by_name[name].narration_seconds > 0 and by_name[name].turnaround_seconds > 0
    assert by_name["scene_list"].scenes == 2

    with (out / "summary.csv").open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert list(rows[0]) == ["input", "job_id", "status", "error_code", "narration_seconds", "turnaround_seconds",
                             "scenes", "warnings_count"]
    assert [r["input"] for r in rows] == ["a_script", "scene_list", "c_voice", "d_empty"]
    summary = (out / "summary.md").read_text(encoding="utf-8")
    assert "succeeded: 3" in summary and "| d_empty |" in summary

    # Running the same batch again re-uses the same jobs (Idempotency-Key from the input itself).
    again = run_batch(items, tmp_path / "out2", client, ratios=["1:1"], concurrency=1, timeout=180, poll=0.05)
    assert [r.job_id for r in again] == [r.job_id for r in results]


def test_batch_run_backs_off_and_respects_retry_after(tmp_path, monkeypatch):
    """A fake API (httpx.MockTransport) that answers 429 + Retry-After first: the runner must wait
    exactly that long and try again, and its pauses between status checks must grow."""
    import httpx

    import tools.batch_run as batch

    sleeps, calls = [], []
    monkeypatch.setattr(batch.time, "sleep", sleeps.append)
    answers = {"POST": [httpx.Response(429, headers={"Retry-After": "7"}, json={"error": {"code": "RATE_LIMITED"}}),
                        httpx.Response(202, json={"job_id": "j1"})],
               "GET": [httpx.Response(200, json={"status": "running"}),
                       httpx.Response(200, json={"status": "running"}),
                       httpx.Response(429, headers={"Retry-After": "9"}, json={"error": {"code": "RATE_LIMITED"}}),
                       httpx.Response(200, json={"status": "running"}),
                       httpx.Response(200, json={"status": "succeeded", "warnings": [], "result": {"files": {}}})]}

    def handler(request):
        calls.append(request.method)
        return answers[request.method].pop(0)

    with httpx.Client(base_url="http://api.test", transport=httpx.MockTransport(handler)) as client:
        item = batch.Item(name="one", source="test", body={"script": "Hi."})
        result = batch.run_one(client, item, tmp_path, None, timeout=600, poll=2.0, max_poll=15.0)

    assert result.status == "succeeded" and result.job_id == "j1"
    assert calls == ["POST", "POST", "GET", "GET", "GET", "GET", "GET"]
    # 7 s = the POST's Retry-After; then 2 s, 3 s growing pauses; the 429 lifts the pause to 9 s; then x1.5.
    assert sleeps == [7.0, 2.0, 3.0, 9.0, 13.5]


def test_cli_without_inputs_exits_with_code_2(tmp_path):
    (tmp_path / "empty").mkdir()
    assert main([str(tmp_path / "empty"), "--out", str(tmp_path / "out")]) == 2
