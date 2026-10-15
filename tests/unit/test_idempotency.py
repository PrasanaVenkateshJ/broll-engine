"""Idempotency-Key: same key + same body -> same job; same key + different body -> 409."""
import json

from sqlalchemy import func, select

from app.db import new_session
from app.jobs import body_hash
from app.models import Job
from tests.conftest import AUTH, OTHER_AUTH
from tests.media_helpers import tone_bytes
from tests.unit.test_errors import assert_error_format

URL = "/api/v1/broll/jobs"


def count_jobs() -> int:
    with new_session() as db:
        return db.scalar(select(func.count()).select_from(Job))


def test_body_hash_ignores_key_order():
    assert body_hash({"a": 1, "b": [1, 2]}) == body_hash({"b": [1, 2], "a": 1})
    assert body_hash({"a": 1}) != body_hash({"a": 2})
    assert body_hash({"a": 1}, "filehash1") != body_hash({"a": 1}, "filehash2")


def test_same_key_same_body_returns_same_job(client):
    headers = {**AUTH, "Idempotency-Key": "key-1"}
    first = client.post(URL, json={"script": "Hello world."}, headers=headers)
    second = client.post(URL, json={"script": "Hello world."}, headers=headers)
    assert first.status_code == second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]
    assert second.headers["Idempotent-Replayed"] == "true"
    assert count_jobs() == 1  # no duplicate work


def test_same_key_with_defaults_written_out_is_same_body(client):
    headers = {**AUTH, "Idempotency-Key": "key-defaults"}
    first = client.post(URL, json={"script": "Hi."}, headers=headers)
    second = client.post(URL, json={"script": "Hi.", "captions": True}, headers=headers)  # True is the default
    assert first.json()["job_id"] == second.json()["job_id"]


def test_same_key_different_body_is_409(client):
    headers = {**AUTH, "Idempotency-Key": "key-2"}
    client.post(URL, json={"script": "First text."}, headers=headers)
    resp = client.post(URL, json={"script": "Different text."}, headers=headers)
    assert resp.status_code == 409
    assert_error_format(resp, "IDEMPOTENCY_KEY_REUSED")
    assert count_jobs() == 1


def test_no_key_means_new_job_each_time(client):
    a = client.post(URL, json={"script": "Same."}, headers=AUTH)
    b = client.post(URL, json={"script": "Same."}, headers=AUTH)
    assert a.json()["job_id"] != b.json()["job_id"]


def test_keys_are_per_client(client):
    a = client.post(URL, json={"script": "One."}, headers={**AUTH, "Idempotency-Key": "shared"})
    b = client.post(URL, json={"script": "Two."}, headers={**OTHER_AUTH, "Idempotency-Key": "shared"})
    assert a.status_code == b.status_code == 202
    assert a.json()["job_id"] != b.json()["job_id"]


def test_upload_idempotency_uses_file_content(client):
    headers = {**AUTH, "Idempotency-Key": "upload-1"}
    opts = {"options": json.dumps({"aspect_ratios": ["16:9"]})}
    a = client.post(URL, files={"audio": ("voice.mp3", tone_bytes(".mp3", 2.0), "audio/mpeg")}, data=opts, headers=headers)
    b = client.post(URL, files={"audio": ("voice.mp3", tone_bytes(".mp3", 2.0), "audio/mpeg")}, data=opts, headers=headers)
    assert a.status_code == b.status_code == 202
    assert a.json()["job_id"] == b.json()["job_id"]

    c = client.post(URL, files={"audio": ("voice.mp3", tone_bytes(".mp3", 3.0), "audio/mpeg")}, data=opts, headers=headers)
    assert c.status_code == 409
    assert count_jobs() == 1
