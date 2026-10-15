"""End-to-end job flow through the API with the real worker threads running."""
import json
import time
from urllib.parse import urlsplit

from app.errors import AppError, ErrorCode
from app.security import verify_hmac
from tests.conftest import AUTH

URL = "/api/v1/broll/jobs"


def wait_for_job(client, job_id, timeout=20):
    """Poll GET /jobs/{id} until it ends. Returns (final body, list of seen (status, progress))."""
    seen = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/v1/jobs/{job_id}", headers=AUTH).json()
        seen.append((body["status"], body["progress"]))
        if body["status"] in ("succeeded", "failed"):
            return body, seen
        time.sleep(0.02)
    raise AssertionError(f"job did not finish in {timeout}s; last={seen[-1]}")


def path_of(url):
    parts = urlsplit(url)
    return f"{parts.path}?{parts.query}"


def test_job_goes_queued_running_succeeded_and_file_downloads(client, worker):
    resp = client.post(URL, json={"script": "Sales grew 40% in 2025."}, headers=AUTH)
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    final, seen = wait_for_job(client, job_id)
    assert final["status"] == "succeeded" and final["progress"] == 100 and final["error"] is None
    assert any(status == "running" and 0 < p < 100 for status, p in seen)  # progress moved
    assert [p for _, p in seen] == sorted(p for _, p in seen)  # never goes backwards

    link = final["result"]["files"]["result.txt"]
    download = client.get(path_of(link))  # NO bearer token: the signature is enough
    assert download.status_code == 200
    assert json.loads(download.content)["job_id"] == job_id


def test_tampered_download_link_is_rejected(client, worker):
    job_id = client.post(URL, json={"script": "Hi."}, headers=AUTH).json()["job_id"]
    final, _ = wait_for_job(client, job_id)
    link = path_of(final["result"]["files"]["result.txt"])
    bad = client.get(link[:-4] + "0000")
    assert bad.status_code == 401
    assert bad.json()["error"]["code"] == "UNAUTHORIZED"


def test_upload_job_succeeds(client, worker):
    resp = client.post(URL, files={"audio": ("voice.wav", b"RIFF-fake-wav", "audio/wav")},
                       data={"options": json.dumps({"aspect_ratios": ["1:1"], "captions": False})}, headers=AUTH)
    assert resp.status_code == 202
    final, _ = wait_for_job(client, resp.json()["job_id"])
    assert final["status"] == "succeeded"
    assert final["input_type"] == "upload"


def test_two_jobs_at_the_same_time_both_finish(client, worker):
    ids = [client.post(URL, json={"script": f"User {i} says hi."}, headers=AUTH).json()["job_id"] for i in range(2)]
    results = [wait_for_job(client, job_id)[0] for job_id in ids]
    assert [r["status"] for r in results] == ["succeeded", "succeeded"]


def test_callback_sent_when_job_finishes(client, worker, callback_receiver):
    receiver = callback_receiver()
    job_id = client.post(URL, json={"script": "Hi.", "callback_url": receiver.url}, headers=AUTH).json()["job_id"]
    wait_for_job(client, job_id)
    deadline = time.time() + 5
    while not receiver.calls and time.time() < deadline:
        time.sleep(0.02)
    call = receiver.calls[0]
    assert verify_hmac("test-callback-secret", call["body"], call["headers"]["X-IncuBrix-Signature"])
    assert receiver.json()["status"] == "succeeded"


def test_failing_pipeline_marks_job_failed_with_error_format(client, worker, monkeypatch):
    import app.worker

    def explode(*args):
        raise AppError(ErrorCode.RENDER_FAILED, "ffmpeg exploded", 500, retryable=True)

    monkeypatch.setattr(app.worker, "run_fake_pipeline", explode)
    job_id = client.post(URL, json={"script": "Hi."}, headers=AUTH).json()["job_id"]
    final, _ = wait_for_job(client, job_id)
    assert final["status"] == "failed"
    assert final["error"] == {"code": "RENDER_FAILED", "message": "ffmpeg exploded", "retryable": True, "details": {}}
    assert final["result"] is None


def test_crash_in_pipeline_becomes_internal_error(client, worker, monkeypatch):
    import app.worker

    monkeypatch.setattr(app.worker, "run_fake_pipeline", lambda *a: 1 / 0)
    job_id = client.post(URL, json={"script": "Hi."}, headers=AUTH).json()["job_id"]
    final, _ = wait_for_job(client, job_id)
    assert final["status"] == "failed" and final["error"]["code"] == "INTERNAL_ERROR"


def test_worker_logs_carry_the_request_correlation_id(client, worker, caplog):
    import logging

    caplog.set_level(logging.INFO)
    resp = client.post(URL, json={"script": "Hi."}, headers={**AUTH, "X-Correlation-Id": "trace-42"})
    wait_for_job(client, resp.json()["job_id"])
    from app.logging_setup import JsonFormatter

    lines = [json.loads(JsonFormatter().format(r)) for r in caplog.records if r.name == "app.worker"]
    started = [line for line in lines if line["message"] == "job started"]
    assert started and started[0]["correlation_id"] == "trace-42"
    assert started[0]["job_id"] == resp.json()["job_id"]
