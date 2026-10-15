"""Every error must use the same JSON format, and every response carries a correlation id."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.errors import AppError, ErrorCode, register_error_handlers
from tests.conftest import AUTH


def assert_error_format(resp, code):
    body = resp.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "retryable", "details"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["retryable"], bool)
    assert isinstance(body["error"]["details"], dict)


def test_unknown_url_returns_json_404(client):
    resp = client.get("/no/such/page")
    assert resp.status_code == 404
    assert_error_format(resp, "NOT_FOUND")


def test_wrong_method_returns_json_405(client):
    resp = client.delete("/health")
    assert resp.status_code == 405
    assert_error_format(resp, "METHOD_NOT_ALLOWED")


def test_validation_error_is_invalid_input(client):
    resp = client.post("/api/v1/broll/jobs", json={"script": "hi", "captions": "not-a-bool"}, headers=AUTH)
    assert resp.status_code == 422
    assert_error_format(resp, "INVALID_INPUT")
    assert resp.json()["error"]["details"]["fields"][0]["loc"] == ["captions"]


def test_unexpected_crash_returns_json_500_with_correlation_id():
    mini = FastAPI()
    register_error_handlers(mini)

    @mini.get("/boom")
    def boom():
        raise RuntimeError("secret internals")

    with TestClient(mini, raise_server_exceptions=False) as c:
        resp = c.get("/boom")
    assert resp.status_code == 500
    assert_error_format(resp, "INTERNAL_ERROR")
    assert "secret internals" not in resp.text  # never leak internals


def test_app_error_carries_status_and_headers():
    mini = FastAPI()
    register_error_handlers(mini)

    @mini.get("/limited")
    def limited():
        raise AppError(ErrorCode.RATE_LIMITED, "slow down", 429, retryable=True, headers={"Retry-After": "5"})

    with TestClient(mini) as c:
        resp = c.get("/limited")
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "5"
    assert_error_format(resp, "RATE_LIMITED")
    assert resp.json()["error"]["retryable"] is True


def test_correlation_id_is_echoed(client):
    resp = client.get("/health", headers={"X-Correlation-Id": "abc-123"})
    assert resp.headers["X-Correlation-Id"] == "abc-123"


def test_correlation_id_is_generated_when_missing(client):
    resp = client.get("/no/such/page")
    assert len(resp.headers["X-Correlation-Id"]) == 32


def test_health_and_ready(client):
    assert client.get("/health").json() == {"status": "ok"}
    ready = client.get("/ready")
    assert ready.status_code == 200
    assert ready.json()["checks"] == {"database": "ok", "storage": "ok", "storage_backend": "local"}


def test_ready_fails_when_storage_is_broken(client, monkeypatch):
    from app.storage import get_storage

    def broken():
        raise OSError("disk gone")

    monkeypatch.setattr(get_storage(), "check", broken)
    resp = client.get("/ready")
    assert resp.status_code == 503
    assert_error_format(resp, "NOT_READY")
    assert resp.json()["error"]["details"]["storage"] == "error: OSError"


def test_json_log_line_has_required_fields(capsys):
    import json
    import logging

    from app.logging_setup import correlation_id_var, job_id_var, setup_logging

    setup_logging("INFO")
    t1, t2 = job_id_var.set("job1"), correlation_id_var.set("cid1")
    try:
        logging.getLogger("test").info("hello")
    finally:
        job_id_var.reset(t1)
        correlation_id_var.reset(t2)
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["message"] == "hello" and line["level"] == "INFO"
    assert line["job_id"] == "job1" and line["correlation_id"] == "cid1"
    assert "timestamp" in line
