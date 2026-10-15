"""HMAC, signed download links, bearer-token auth and the rate limit."""
import time

from app.security import RateLimiter, check_file_link, hmac_sha256_hex, sign_file_link, verify_hmac
from tests.conftest import AUTH
from tests.unit.test_errors import assert_error_format


def test_hmac_matches_known_value():
    # Standard test vector for HMAC-SHA256 (Wikipedia).
    assert hmac_sha256_hex("key", b"The quick brown fox jumps over the lazy dog") == (
        "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8"
    )


def test_verify_hmac_rejects_changed_body_or_wrong_secret():
    sig = hmac_sha256_hex("s3cret", b'{"status":"succeeded"}')
    assert verify_hmac("s3cret", b'{"status":"succeeded"}', sig)
    assert not verify_hmac("s3cret", b'{"status":"failed"}', sig)
    assert not verify_hmac("other", b'{"status":"succeeded"}', sig)
    assert not verify_hmac("s3cret", b'{"status":"succeeded"}', "")


def test_signed_link_valid_tampered_and_expired():
    import pytest

    from app.errors import AppError

    expires, sig = sign_file_link("job1", "result.txt")
    check_file_link("job1", "result.txt", expires, sig)  # no error = valid

    with pytest.raises(AppError, match="signature"):
        check_file_link("job1", "other.txt", expires, sig)  # different file
    with pytest.raises(AppError, match="signature"):
        check_file_link("job1", "result.txt", expires + 9999, sig)  # someone extended the expiry

    old_expires, old_sig = sign_file_link("job1", "result.txt", now=time.time() - 7200)
    with pytest.raises(AppError, match="expired"):
        check_file_link("job1", "result.txt", old_expires, old_sig)


def test_api_needs_bearer_token(client):
    resp = client.get("/api/v1/jobs/abc")
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"
    assert_error_format(resp, "UNAUTHORIZED")

    resp = client.get("/api/v1/jobs/abc", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401

    resp = client.get("/api/v1/jobs/abc", headers=AUTH)
    assert resp.status_code == 404  # token accepted; job just does not exist
    assert_error_format(resp, "JOB_NOT_FOUND")


def test_open_routes_need_no_token(client):
    for path in ("/health", "/ready", "/docs", "/openapi.json"):
        assert client.get(path).status_code == 200, path


def test_auth_is_off_when_no_tokens_configured(client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("API_TOKENS", "")
    get_settings.cache_clear()
    assert client.get("/api/v1/jobs/abc").status_code == 404  # not 401


def test_rate_limiter_counts_per_client_per_minute():
    rl = RateLimiter()
    t = 1_000_000 * 60.0  # start of a minute
    assert [rl.hit("a", 2, now=t) for _ in range(2)] == [None, None]
    assert rl.hit("a", 2, now=t + 1) is not None  # 3rd in the same minute -> blocked
    assert rl.hit("b", 2, now=t + 1) is None  # other client unaffected
    assert rl.hit("a", 2, now=t + 61) is None  # next minute -> allowed again


def test_rate_limit_returns_429(client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "3")
    get_settings.cache_clear()
    codes = [client.get("/api/v1/jobs/abc", headers=AUTH).status_code for _ in range(4)]
    assert codes == [404, 404, 404, 429]
    resp = client.get("/api/v1/jobs/abc", headers=AUTH)
    assert_error_format(resp, "RATE_LIMITED")
    assert int(resp.headers["Retry-After"]) > 0
