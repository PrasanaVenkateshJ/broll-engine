"""Live-demo guards: the daily job cap, re-queueing jobs cut off by a restart, the worker's after_job hook
and the SQLite journal setting. Mock adapters, no keys."""
import threading
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from app.config import get_settings
from app.db import get_engine, new_session
from app.errors import AppError
from app.jobs import check_daily_limit, claim_next_job, recover_interrupted_jobs
from app.models import Job
from tests.conftest import AUTH, OTHER_AUTH
from tests.unit.test_errors import assert_error_format

URL = "/api/v1/broll/jobs"


def cap(monkeypatch, n: int):
    monkeypatch.setenv("DEMO_MAX_JOBS_PER_DAY", str(n))
    get_settings.cache_clear()


def test_daily_cap_answers_429_with_retry_after_and_counts_all_tokens(client, monkeypatch):
    cap(monkeypatch, 2)
    assert client.post(URL, json={"script": "One."}, headers=AUTH).status_code == 202
    assert client.post(URL, json={"script": "Two."}, headers=OTHER_AUTH).status_code == 202  # all tokens count
    resp = client.post(URL, json={"script": "Three."}, headers=AUTH)
    assert resp.status_code == 429
    assert_error_format(resp, "DAILY_LIMIT_REACHED")
    err = resp.json()["error"]
    assert err["retryable"] is True
    assert err["details"]["limit_per_day"] == 2 and err["details"]["used_today"] == 2
    assert 0 < int(resp.headers["Retry-After"]) <= 86400
    with new_session() as db:
        assert db.query(Job).count() == 2  # nothing was queued


def test_idempotent_replay_still_works_when_the_cap_is_reached(client, monkeypatch):
    cap(monkeypatch, 1)
    first = client.post(URL, json={"script": "One."}, headers={**AUTH, "Idempotency-Key": "k1"})
    again = client.post(URL, json={"script": "One."}, headers={**AUTH, "Idempotency-Key": "k1"})
    assert first.status_code == again.status_code == 202
    assert again.json()["job_id"] == first.json()["job_id"]
    assert again.headers["Idempotent-Replayed"] == "true"


def test_cap_zero_means_no_cap(client):
    for i in range(5):
        assert client.post(URL, json={"script": f"Line {i}."}, headers=AUTH).status_code == 202


def test_only_todays_jobs_count(monkeypatch):
    cap(monkeypatch, 1)
    with new_session() as db:
        db.add(Job(id="old", client_id="c", input_type="script", request_json="{}",
                   created_at=datetime(2026, 10, 15, 23, 59, tzinfo=timezone.utc)))
        db.commit()
        check_daily_limit(db, now=datetime(2026, 10, 16, 0, 1, tzinfo=timezone.utc))  # yesterday's job: ok
        with pytest.raises(AppError) as e:
            check_daily_limit(db, now=datetime(2026, 10, 15, 23, 59, 30, tzinfo=timezone.utc))
    assert e.value.headers["Retry-After"] == "30"  # seconds until midnight UTC
    assert e.value.details["resets_at"] == "2026-10-16T00:00:00+00:00"


def test_a_job_cut_off_by_a_restart_is_queued_once_then_failed(client):
    job_id = client.post(URL, json={"script": "Hello."}, headers=AUTH).json()["job_id"]
    assert claim_next_job("old-container") == job_id  # now "running" in a container that then vanished

    assert recover_interrupted_jobs() == {"requeued": [job_id], "failed": []}
    body = client.get(f"/api/v1/jobs/{job_id}", headers=AUTH).json()
    assert body["status"] == "queued" and body["progress"] == 0
    assert any("restarted" in w for w in body["warnings"])

    assert claim_next_job("second-container") == job_id  # cut off again
    assert recover_interrupted_jobs() == {"requeued": [], "failed": [job_id]}
    body = client.get(f"/api/v1/jobs/{job_id}", headers=AUTH).json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "JOB_INTERRUPTED" and body["error"]["retryable"] is True


def test_recovery_frees_a_cut_off_swaps_lock(client):
    with new_session() as db:
        db.add(Job(id="root", client_id="c", status="succeeded", input_type="script", request_json="{}",
                   active_swap_id="swap1", requeue_count=0))
        db.add(Job(id="swap1", client_id="c", status="running", input_type="swap", request_json="{}",
                   parent_job_id="root", requeue_count=1))
        db.commit()
    assert recover_interrupted_jobs()["failed"] == ["swap1"]
    with new_session() as db:
        assert db.get(Job, "root").active_swap_id is None


def test_after_job_hook_runs_after_every_job(client):
    from app.worker import start_workers

    done = []
    finished = threading.Event()

    def hook(job_id):
        done.append(job_id)
        finished.set()

    job_id = client.post(URL, json={"script": "Small shops open early."}, headers=AUTH).json()["job_id"]
    stop = threading.Event()
    threads = start_workers(stop, after_job=hook)
    try:
        assert finished.wait(120), "the job did not finish"
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=10)
    assert done == [job_id]


def test_sqlite_journal_mode_comes_from_the_setting(monkeypatch):
    from app.db import reset_engine

    with get_engine().connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar().lower() == "wal"
    monkeypatch.setenv("SQLITE_JOURNAL_MODE", "DELETE")
    get_settings.cache_clear()
    reset_engine()
    with get_engine().connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar().lower() == "delete"


def test_old_database_gets_the_requeue_count_column(tmp_path, monkeypatch):
    """A database made before requeue_count existed is upgraded by init_db (ALTER TABLE)."""
    import sqlite3

    from app.db import init_db, reset_engine

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE jobs (id VARCHAR(32) PRIMARY KEY, client_id VARCHAR(64), status VARCHAR(16), "
                     "progress INTEGER, stage VARCHAR(32), input_type VARCHAR(16), request_json TEXT)")
        conn.execute("INSERT INTO jobs VALUES ('j', 'c', 'running', 5, 'x', 'script', '{}')")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path.as_posix()}")
    get_settings.cache_clear()
    reset_engine()
    init_db()
    with get_engine().connect() as conn:
        assert conn.execute(text("SELECT requeue_count FROM jobs WHERE id='j'")).scalar() == 0
