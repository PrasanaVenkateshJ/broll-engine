"""Security helpers: HMAC signatures, signed download links, bearer-token auth, rate limit.

HMAC = a "fingerprint" of a message made with a secret key. Only someone with the same
secret can make the same fingerprint, so the receiver can prove the message is genuine
and was not changed.
"""
import hashlib
import hmac
import ipaddress
import socket
import threading
import time
from urllib.parse import urlsplit

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
    """Fixed window: each key may make N requests per calendar minute.

    The key is "<client>:<bucket>", so job creation (POST) and status checks (GET) are
    counted separately. Kept in memory, so it is per API process (fine for one api container).
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
    check_rate_limit(client, "post" if request.method == "POST" else "get")
    return client


def check_rate_limit(client: str, bucket: str) -> None:
    """Count one request of `client` in `bucket` ("post" = new jobs + swaps, "get" = status + files).

    Two limits because the work behind them is so different: a POST can start minutes of
    rendering, a GET is a cheap database read that clients repeat while they wait.
    Over the limit -> 429 RATE_LIMITED with a Retry-After header (seconds to wait).
    """
    s = get_settings()
    limit = s.RATE_LIMIT_POST_PER_MINUTE if bucket == "post" else s.RATE_LIMIT_GET_PER_MINUTE
    if limit <= 0:  # 0 = no limit
        return
    retry_after = rate_limiter.hit(f"{client}:{bucket}", limit)
    if retry_after is not None:
        raise AppError(ErrorCode.RATE_LIMITED, f"Too many {bucket.upper()} requests; retry after {retry_after} s",
                       429, retryable=True,
                       details={"bucket": bucket, "limit_per_minute": limit, "retry_after_seconds": retry_after},
                       headers={"Retry-After": str(retry_after)})


# ---------- SSRF guard ----------
# SSRF = "server-side request forgery": a user gives us a URL like http://169.254.169.254/
# (cloud secrets) or http://localhost:9000 (our own MinIO), and our server fetches it for
# them. So before fetching any URL we did not choose ourselves, we look up its IP
# addresses and refuse private / loopback / link-local ones.

class BlockedURL(Exception):
    """The URL is not allowed (bad scheme, or it points into a private network)."""


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:  # ::ffff:127.0.0.1 is really 127.0.0.1
        ip = ip.ipv4_mapped
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified)


def check_public_url(url: str) -> None:
    """Raise BlockedURL unless url is http(s) and every address its host resolves to is public.

    ALLOW_PRIVATE_URLS=true switches the address check off (tests use local servers).
    Limitation: the host is looked up again when we connect, so a DNS server that changes
    its answer in between ("DNS rebinding") is not stopped by this check alone.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise BlockedURL("only http and https URLs are allowed")
    if get_settings().ALLOW_PRIVATE_URLS:
        return
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80),
                                   type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as e:
        raise BlockedURL(f"host {parts.hostname} could not be resolved") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])  # drop an IPv6 zone like "%eth0"
        if not _is_public_ip(ip):
            raise BlockedURL(f"host {parts.hostname} points to a private or local address")
