"""One safe way to download a file from the internet (audio_url, footage, preview images).

Every download goes through fetch_to_file(), so all of them get the same protections:
- SSRF guard: the URL (and every redirect it leads to) must point to a public address,
- a size limit (stop as soon as the file is too big),
- a total time limit.
Errors are raised as DownloadError with a `reason`; each caller turns that into its own
error code (intake -> DOWNLOAD_FAILED, footage -> "try the next clip").
"""
import time
from pathlib import Path
from urllib.parse import urljoin

import httpx

from app.security import BlockedURL, check_public_url

MB = 1024 * 1024
MAX_REDIRECTS = 5


class DownloadError(Exception):
    """reason: blocked | http_status | too_large | timeout | network | empty"""

    def __init__(self, message: str, reason: str, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.reason = reason
        self.status = status
        self.retryable = retryable


def fetch_to_file(url: str, dest: Path, max_bytes: int, timeout: float, headers: dict | None = None) -> Path:
    """Download url to dest. Redirects are followed by hand so each new address is checked too."""
    deadline = time.monotonic() + timeout
    size = 0
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, headers=headers) as client:
            for _ in range(MAX_REDIRECTS + 1):
                try:
                    check_public_url(url)
                except BlockedURL as e:
                    raise DownloadError(str(e), "blocked") from None
                with client.stream("GET", url) as resp:
                    if resp.is_redirect:
                        url = urljoin(url, resp.headers.get("location", ""))
                        continue
                    if resp.status_code != 200:
                        raise DownloadError(f"HTTP {resp.status_code}", "http_status", resp.status_code,
                                            retryable=resp.status_code >= 500)
                    declared = resp.headers.get("content-length", "")
                    if declared.isdigit() and int(declared) > max_bytes:
                        raise DownloadError("file too large", "too_large")  # stop before downloading anything
                    with dest.open("wb") as out:
                        for chunk in resp.iter_bytes(MB):
                            size += len(chunk)
                            if size > max_bytes:
                                raise DownloadError("file too large", "too_large")
                            if time.monotonic() > deadline:
                                raise DownloadError(f"took longer than {timeout:g} s", "timeout", retryable=True)
                            out.write(chunk)
                    break
            else:
                raise DownloadError("too many redirects", "network")
    except httpx.HTTPError as e:
        dest.unlink(missing_ok=True)
        raise DownloadError(type(e).__name__, "network", retryable=True) from None
    except DownloadError:
        dest.unlink(missing_ok=True)
        raise
    if size == 0:
        dest.unlink(missing_ok=True)
        raise DownloadError("the downloaded file is empty", "empty")
    return dest
