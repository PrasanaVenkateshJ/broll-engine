"""The tiny migration in init_db(): an OLD database file gets the newer columns."""
import sqlite3

from sqlalchemy import inspect

from app.config import get_settings
from app.db import get_engine, init_db, reset_engine


def test_old_database_gets_the_parent_job_id_column(tmp_path, monkeypatch):
    old = tmp_path / "old.db"
    with sqlite3.connect(old) as conn:  # the jobs table as it was before clip swap existed
        conn.execute("CREATE TABLE jobs (id VARCHAR(32) PRIMARY KEY, client_id VARCHAR(64), status VARCHAR(16), "
                     "progress INTEGER, stage VARCHAR(32), input_type VARCHAR(16), request_json TEXT, "
                     "input_key VARCHAR(255), callback_url TEXT, callback_status VARCHAR(16), "
                     "callback_attempts INTEGER, warnings_json TEXT, error_json TEXT, result_files_json TEXT, "
                     "worker_id VARCHAR(64), correlation_id VARCHAR(128), created_at DATETIME, "
                     "updated_at DATETIME, started_at DATETIME, finished_at DATETIME)")
        conn.execute("INSERT INTO jobs (id, client_id, status, input_type, request_json) "
                     "VALUES ('old1', 'c', 'succeeded', 'script', '{}')")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{old.as_posix()}")
    get_settings.cache_clear()
    reset_engine()
    init_db()
    init_db()  # running it twice is fine
    columns = {c["name"] for c in inspect(get_engine()).get_columns("jobs")}
    assert "parent_job_id" in columns
    with sqlite3.connect(old) as conn:  # the old row is still there
        assert conn.execute("SELECT id, parent_job_id FROM jobs").fetchall() == [("old1", None)]
    reset_engine()
