"""HMAC, signed download links, bearer-token auth and the rate limit."""
import time

from app.security import RateLimiter, check_file_link, hmac_sha256_hex, sign_file_link, verify_hmac
from tests.conftest import AUTH, OTHER_AUTH
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


def set_limits(monkeypatch, post, get):
    """The test fixture switches the rate limit off; these tests switch it back on with small limits."""
    from app.config import get_settings

    monkeypatch.setenv("RATE_LIMIT_POST_PER_MINUTE", str(post))
    monkeypatch.setenv("RATE_LIMIT_GET_PER_MINUTE", str(get))
    get_settings.cache_clear()


def test_rate_limit_returns_429_with_retry_after(client, monkeypatch):
    set_limits(monkeypatch, post=0, get=3)
    codes = [client.get("/api/v1/jobs/abc", headers=AUTH).status_code for _ in range(4)]
    assert codes == [404, 404, 404, 429]
    resp = client.get("/api/v1/jobs/abc", headers=AUTH)
    assert_error_format(resp, "RATE_LIMITED")
    assert resp.json()["error"]["retryable"] is True
    assert 0 < int(resp.headers["Retry-After"]) <= 61
    assert resp.json()["error"]["details"]["bucket"] == "get"


def test_post_and_get_limits_are_counted_separately(client, monkeypatch):
    set_limits(monkeypatch, post=2, get=3)
    posts = [client.post("/api/v1/broll/jobs", json={"script": f"Hi {i}."}, headers=AUTH) for i in range(3)]
    assert [r.status_code for r in posts] == [202, 202, 429]
    assert posts[2].json()["error"]["details"] == {"bucket": "post", "limit_per_minute": 2,
                                                    "retry_after_seconds": int(posts[2].headers["Retry-After"])}
    # POSTs used up, but status checks still work (their own, larger budget).
    job_id = posts[0].json()["job_id"]
    assert [client.get(f"/api/v1/jobs/{job_id}", headers=AUTH).status_code for _ in range(3)] == [200, 200, 200]
    assert client.get(f"/api/v1/jobs/{job_id}", headers=AUTH).status_code == 429
    # Another token has its own budgets.
    assert client.get(f"/api/v1/jobs/{job_id}", headers=OTHER_AUTH).status_code == 404


def test_file_downloads_count_against_the_owners_get_limit(client, monkeypatch):
    from app.jobs import finish_job, sign_file_link

    set_limits(monkeypatch, post=0, get=1)
    job_id = client.post("/api/v1/broll/jobs", json={"script": "Hi."}, headers=AUTH).json()["job_id"]
    finish_job(job_id, files=["timeline.json"])
    expires, sig = sign_file_link(job_id, "timeline.json")
    link = f"/api/v1/jobs/{job_id}/files/timeline.json?expires={expires}&sig={sig}"
    assert client.get(link).status_code == 404  # 1st GET allowed (the file itself is not in storage)
    resp = client.get(link)                      # 2nd GET of the same owner this minute
    assert resp.status_code == 429 and int(resp.headers["Retry-After"]) > 0
