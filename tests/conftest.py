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
        "MAX_UPLOAD_MB": "5",
        # Pipeline: mock adapters only (no models, no keys), fastest video encoding.
        # Set here so a real .env on the PC (e.g. TTS_PROVIDER=kokoro) never leaks into tests.
        "TTS_PROVIDER": "mock",
        "TRANSCRIBE_PROVIDER": "mock",
        "VIDEO_PRESET": "ultrafast",
        "KEEP_WORK_FILES": "false",
        "MAX_AUDIO_SECONDS": "600",
        "MAX_DOWNLOAD_MB": "5",
        "DOWNLOAD_TIMEOUT_SECONDS": "5",
        "MIN_SCENE_SECONDS": "2.0",
        "MAX_SCENE_SECONDS": "8.0",
        "PAUSE_SECONDS": "0.35",
        "CAPTION_MAX_CHARS_PER_LINE": "42",
        "CAPTION_MAX_LINES": "2",
        # Phases 5-7: mock understanding, mock footage + generated cards, mock scorer, no keys.
        "UNDERSTAND_PROVIDER": "mock",
        "GROQ_API_KEY": "",
        "PIXABAY_API_KEY": "",
        "PEXELS_API_KEY": "",
        "PROVIDER_ORDER": "mock,generated",
        "PROVIDERS_DISABLED": "",
        "PROVIDERS_OVERRIDE_FILE": str(tmp_path / "providers_override.json"),
        "RANK_SCORER": "mock",
        "STOCK_MIN_SCORE": "0.22",
        "GENERATED_FOR_STATISTICS": "true",
        "QA_RETRIES": "1",
        "SEED": "1234",
        "LLM_TEMPERATURE": "0",
        "LOGO_CHECK": "true",
        "LOGO_REJECT_MARGIN": "-0.04",
        "LOGO_PENALTY_MARGIN": "-0.06",
        "LOGO_PENALTY": "0.03",
        "LOGO_FRAME_COUNT": "3",
        "CROP_SAMPLE_FRAMES": "3",
        "CROP_BLUR_MARGIN": "0.02",
        "CROP_TIE_MARGIN": "0.03",
        # Tests use small local web servers (127.0.0.1); the SSRF test switches this back off.
        "ALLOW_PRIVATE_URLS": "true",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    # Mock footage provider back to normal (tests switch on "down" / "corrupted" behaviour).
    from app.pipeline.providers.mock import MockProvider

    monkeypatch.setattr(MockProvider, "fail", False)
    monkeypatch.setattr(MockProvider, "corrupt_ids", set())
    monkeypatch.setattr(MockProvider, "clip_seconds", 6.0)
    monkeypatch.setattr(MockProvider, "logo_scores", {})
    monkeypatch.setattr(MockProvider, "frame_logo_scores", {})
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
