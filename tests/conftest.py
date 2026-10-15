"""Shared test setup.

Every test gets a FRESH temporary database + local storage folder, test tokens and
fast timings. No API keys, no Docker, no network needed.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import init_db, reset_engine
from app.security import rate_limiter
from app.storage import reset_storage

TOKEN = "test-token"
OTHER_TOKEN = "other-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
OTHER_AUTH = {"Authorization": f"Bearer {OTHER_TOKEN}"}


def _reset_singletons():
    get_settings.cache_clear()
    reset_engine()
    reset_storage()
    rate_limiter.reset()


@pytest.fixture(autouse=True)
def test_env(tmp_path, monkeypatch):
    env = {
        "DATABASE_URL": f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        "STORAGE_BACKEND": "local",
        "LOCAL_STORAGE_DIR": str(tmp_path / "storage"),
        "WORK_DIR": str(tmp_path / "work"),
        "API_TOKENS": f"{TOKEN},{OTHER_TOKEN}",
        "RATE_LIMIT_PER_MINUTE": "1000",
        "PUBLIC_BASE_URL": "",
        "FILE_LINK_SECRET": "test-link-secret",
        "FILE_LINK_TTL_SECONDS": "3600",
        "CALLBACK_SECRET": "test-callback-secret",
        "CALLBACK_MAX_RETRIES": "3",
        "CALLBACK_BACKOFF_SECONDS": "0.01",
        "CALLBACK_TIMEOUT_SECONDS": "2",
        "WORKER_CONCURRENCY": "2",
        "WORKER_POLL_SECONDS": "0.05",
        "FAKE_STAGE_SECONDS": "0.05",
        "MAX_UPLOAD_MB": "5",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    _reset_singletons()
    init_db()
    yield
    _reset_singletons()


@pytest.fixture
def client():
    with TestClient(app_instance()) as c:
        yield c


def app_instance():
    from app.main import app

    return app


@pytest.fixture
def worker():
    """Run the real worker threads inside the test process."""
    from app.worker import start_workers

    stop = threading.Event()
    threads = start_workers(stop)
    yield
    stop.set()
    for t in threads:
        t.join(timeout=10)


class CallbackReceiver:
    """A tiny local web server that records callbacks. `responses` = status codes to answer in order."""

    def __init__(self, responses=(200,)):
        self.calls: list[dict] = []
        self.responses = list(responses)
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                receiver.calls.append({"headers": dict(self.headers), "body": body})
                code = receiver.responses.pop(0) if len(receiver.responses) > 1 else receiver.responses[0]
                self.send_response(code)
                self.end_headers()

            def log_message(self, *args):  # keep test output quiet
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/hook"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def json(self, i=0):
        return json.loads(self.calls[i]["body"])

    def close(self):
        self.server.shutdown()


@pytest.fixture
def callback_receiver():
    receivers = []

    def make(responses=(200,)):
        r = CallbackReceiver(responses)
        receivers.append(r)
        return r

    yield make
    for r in receivers:
        r.close()
