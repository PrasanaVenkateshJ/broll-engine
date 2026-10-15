"""Security helpers: HMAC signatures, signed download links, bearer-token auth, rate limit.

HMAC = a "fingerprint" of a message made with a secret key. Only someone with the same
secret can make the same fingerprint, so the receiver can prove the message is genuine
and was not changed.
"""
import hashlib
import hmac
import threading
import time

from fastapi import Request

from app.config import get_settings
from app.errors import AppError, ErrorCode


def hmac_sha256_hex(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify_hmac(secret: str, message: bytes, signature: str) -> bool:
    # compare_digest takes the same time whether 1 or 20 characters match (no timing leaks).
    return hmac.compare_digest(hmac_sha256_hex(secret, message), signature or "")


# ---------- Signed download links ----------

def _link_message(job_id: str, filename: str, expires: int) -> bytes:
    return f"{job_id}/{filename}:{expires}".encode()


def sign_file_link(job_id: str, filename: str, now: float | None = None) -> tuple[int, str]:
    """Returns (expires_unix_time, signature) for one file of one job."""
    s = get_settings()
    expires = int((now if now is not None else time.time()) + s.FILE_LINK_TTL_SECONDS)
    return expires, hmac_sha256_hex(s.FILE_LINK_SECRET, _link_message(job_id, filename, expires))


def check_file_link(job_id: str, filename: str, expires: int, signature: str) -> None:
    """Raises UNAUTHORIZED if the link was tampered with or is too old."""
    s = get_settings()
    if not verify_hmac(s.FILE_LINK_SECRET, _link_message(job_id, filename, expires), signature):
        raise AppError(ErrorCode.UNAUTHORIZED, "Invalid download link signature", 401)
    if expires < time.time():
        raise AppError(ErrorCode.UNAUTHORIZED, "Download link has expired; fetch the job again for a fresh link", 401)


# ---------- Auth + rate limit ----------

def client_id_for(token: str | None) -> str:
    """A short, non-secret id for a token (we never store or log the token itself)."""
    if not token:
        return "anonymous"
    return hashlib.sha256(token.encode()).hexdigest()[:16]


class RateLimiter:
    """Fixed window: each client may make N requests per calendar minute.

    Kept in memory, so it is per API process (fine for one api container).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._windows: dict[str, tuple[int, int]] = {}  # client -> (minute number, count)

    def hit(self, client: str, limit: int, now: float | None = None) -> int | None:
        """Count one request. Returns None if allowed, else seconds until the next window."""
        now = now if now is not None else time.time()
        minute = int(now // 60)
        with self._lock:
            window, count = self._windows.get(client, (minute, 0))
            if window != minute:
                window, count = minute, 0
            if count >= limit:
                return int(60 - (now % 60)) + 1
            self._windows[client] = (window, count + 1)
            return None

    def reset(self) -> None:
        with self._lock:
            self._windows.clear()


rate_limiter = RateLimiter()


def require_client(request: Request) -> str:
    """FastAPI dependency for all /api/v1 routes: check the bearer token + rate limit.

    Returns the client id. If API_TOKENS is empty, auth is off and everyone is "anonymous".
    """
    s = get_settings()
    tokens = s.api_tokens
    token = None
    if tokens:
        header = request.headers.get("Authorization", "")
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer" or not value.strip():
            raise AppError(ErrorCode.UNAUTHORIZED, "Missing bearer token", 401,
                           headers={"WWW-Authenticate": "Bearer"})
        token = value.strip()
        if not any(hmac.compare_digest(token, t) for t in tokens):
            raise AppError(ErrorCode.UNAUTHORIZED, "Invalid bearer token", 401,
                           headers={"WWW-Authenticate": "Bearer"})

    client = client_id_for(token)
    if s.RATE_LIMIT_PER_MINUTE > 0:
        retry_after = rate_limiter.hit(client, s.RATE_LIMIT_PER_MINUTE)
        if retry_after is not None:
            raise AppError(ErrorCode.RATE_LIMITED, "Too many requests; slow down", 429, retryable=True,
                           details={"limit_per_minute": s.RATE_LIMIT_PER_MINUTE},
                           headers={"Retry-After": str(retry_after)})
    return client
