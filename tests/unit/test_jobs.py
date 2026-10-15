"""Job creation rules, the atomic claim and the signed callback (no worker running here)."""
import json
import threading

from app.db import new_session
from app.jobs import claim_next_job, send_callback
from app.models import Job
from app.security import verify_hmac
from tests.conftest import AUTH, OTHER_AUTH
from tests.unit.test_errors import assert_error_format

URL = "/api/v1/broll/jobs"


def test_json_job_returns_202_with_links(client):
    resp = client.post(URL, json={"script": "Cloud computing changed everything.", "aspect_ratios": ["9:16"]},
                       headers=AUTH)
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    assert body["links"]["self"].endswith(f"/api/v1/jobs/{body['job_id']}")
    assert resp.headers["Location"] == body["links"]["self"]


def test_all_three_json_input_types_are_accepted(client):
    for payload, kind in [
        ({"audio_url": "https://example.com/voice.mp3"}, "audio_url"),
        ({"script": "Hello."}, "script"),
        ({"scenes": [{"text": "Hello.", "visual_hint": "sunrise"}]}, "scenes"),
    ]:
        resp = client.post(URL, json=payload, headers=AUTH)
        assert resp.status_code == 202, resp.text
        assert client.get(f"/api/v1/jobs/{resp.json()['job_id']}", headers=AUTH).json()["input_type"] == kind


def test_exactly_one_input_type(client):
    none = client.post(URL, json={"voice": "en-US-female"}, headers=AUTH)
    two = client.post(URL, json={"script": "a", "audio_url": "https://example.com/a.mp3"}, headers=AUTH)
    for resp in (none, two):
        assert resp.status_code == 422
        assert_error_format(resp, "INVALID_INPUT")


def test_unknown_field_rejected(client):
    resp = client.post(URL, json={"script": "a", "colour": "red"}, headers=AUTH)
    assert resp.status_code == 422


def test_bad_aspect_ratio(client):
    resp = client.post(URL, json={"script": "a", "aspect_ratios": ["16:9", "4:3"]}, headers=AUTH)
    assert resp.status_code == 422
    assert_error_format(resp, "UNSUPPORTED_ASPECT_RATIO")


def test_invalid_json_body(client):
    resp = client.post(URL, content=b"{not json", headers={**AUTH, "Content-Type": "application/json"})
    assert resp.status_code == 422
    assert_error_format(resp, "INVALID_INPUT")


def test_upload_rules(client):
    ok = client.post(URL, files={"audio": ("v.m4a", b"abc", "audio/mp4")}, headers=AUTH)
    assert ok.status_code == 202

    bad_type = client.post(URL, files={"audio": ("v.txt", b"abc", "text/plain")}, headers=AUTH)
    assert bad_type.status_code == 415
    assert_error_format(bad_type, "UNSUPPORTED_FILE_TYPE")

    too_big = client.post(URL, files={"audio": ("v.mp3", b"x" * (6 * 1024 * 1024), "audio/mpeg")}, headers=AUTH)
    assert too_big.status_code == 413  # MAX_UPLOAD_MB=5 in tests
    assert_error_format(too_big, "FILE_TOO_LARGE")

    no_file = client.post(URL, data={"options": "{}"}, files={"other": ("v.mp3", b"a")}, headers=AUTH)
    assert no_file.status_code == 422

    # Options may NOT contain another input (exactly one input type).
    two_inputs = client.post(URL, files={"audio": ("v.mp3", b"a", "audio/mpeg")},
                             data={"options": json.dumps({"script": "hi"})}, headers=AUTH)
    assert two_inputs.status_code == 422

    bad_ratio = client.post(URL, files={"audio": ("v.mp3", b"a", "audio/mpeg")},
                            data={"options": json.dumps({"aspect_ratios": ["4:3"]})}, headers=AUTH)
    assert_error_format(bad_ratio, "UNSUPPORTED_ASPECT_RATIO")


def test_other_clients_cannot_see_my_job(client):
    job_id = client.post(URL, json={"script": "Mine."}, headers=AUTH).json()["job_id"]
    assert client.get(f"/api/v1/jobs/{job_id}", headers=AUTH).status_code == 200
    resp = client.get(f"/api/v1/jobs/{job_id}", headers=OTHER_AUTH)
    assert resp.status_code == 404
    assert_error_format(resp, "JOB_NOT_FOUND")


def test_two_workers_never_claim_the_same_job(client):
    ids = {client.post(URL, json={"script": f"Job {i}."}, headers=AUTH).json()["job_id"] for i in range(10)}
    claimed: list[str] = []
    lock = threading.Lock()

    def grab(worker_id):
        while (job_id := claim_next_job(worker_id)) is not None:
            with lock:
                claimed.append(job_id)

    threads = [threading.Thread(target=grab, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(claimed) == sorted(ids)  # every job claimed exactly once
    assert len(claimed) == len(set(claimed))


def _queued_job_with_callback(client, url):
    return client.post(URL, json={"script": "Hi.", "callback_url": url}, headers=AUTH).json()["job_id"]


def test_callback_is_signed(client, callback_receiver):
    receiver = callback_receiver()
    job_id = _queued_job_with_callback(client, receiver.url)
    assert send_callback(job_id) is True
    call = receiver.calls[0]
    assert verify_hmac("test-callback-secret", call["body"], call["headers"]["X-IncuBrix-Signature"])
    assert receiver.json()["job_id"] == job_id


def test_callback_retries_then_succeeds(client, callback_receiver):
    receiver = callback_receiver(responses=(500, 503, 200))
    job_id = _queued_job_with_callback(client, receiver.url)
    waits = []
    assert send_callback(job_id, sleep=waits.append) is True
    assert len(receiver.calls) == 3
    assert waits == [0.01, 0.02]  # backoff doubles each time
    with new_session() as db:
        job = db.get(Job, job_id)
        assert (job.callback_status, job.callback_attempts) == ("delivered", 3)


def test_callback_gives_up_after_three_retries(client, callback_receiver):
    receiver = callback_receiver(responses=(500,))
    job_id = _queued_job_with_callback(client, receiver.url)
    assert send_callback(job_id, sleep=lambda s: None) is False
    assert len(receiver.calls) == 4  # first try + 3 retries
    with new_session() as db:
        assert db.get(Job, job_id).callback_status == "failed"
