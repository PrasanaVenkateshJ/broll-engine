"""Caches for the footage stage, so the same search or picture is never fetched twice.

1. Search results, per job: SelectionContext.cache (rank.py) - always on.
2. Search results, across jobs: a small in-memory table in this worker process, kept for
   FOOTAGE_CACHE_SECONDS, keyed by (provider, query, orientation, results per query).
   Pixabay asks API users to cache results; a second job with the same script asks the API 0 times.
3. Preview pictures, across jobs: files in FOOTAGE_CACHE_DIR/previews, named by a hash of the
   picture's URL, reused while younger than FOOTAGE_CACHE_SECONDS.

Only providers with cache_across_jobs=True use 2 and 3 (real APIs; not the mock test source).
Errors are never cached across jobs: a provider that failed once is asked again by the next job.

single_flight(key): when several threads want the SAME thing at the same time, one fetches it
and the others wait for that result, instead of all downloading it.
"""
import hashlib
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from app.config import get_settings

_search_results: dict[tuple, tuple[float, list]] = {}
_table_lock = threading.Lock()
_key_locks: dict[object, threading.Lock] = {}


@contextmanager
def single_flight(key):
    """Only one thread at a time works on `key`; the others wait (then find the cached result)."""
    with _table_lock:
        lock = _key_locks.setdefault(key, threading.Lock())
    with lock:
        yield


def _ttl() -> float:
    return float(get_settings().FOOTAGE_CACHE_SECONDS)


def get_search(key: tuple) -> list | None:
    """Cached search results (a copy of the list) if still fresh, else None."""
    with _table_lock:
        hit = _search_results.get(key)
    if hit is None or time.monotonic() - hit[0] > _ttl():
        return None
    return list(hit[1])


def put_search(key: tuple, results: list) -> None:
    if _ttl() > 0:
        with _table_lock:
            _search_results[key] = (time.monotonic(), list(results))


def preview_file(url: str) -> Path:
    """Where the cached copy of a preview picture lives (one file per URL)."""
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:24]
    return Path(get_settings().FOOTAGE_CACHE_DIR) / "previews" / f"{digest}.jpg"


def fresh(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0 and time.time() - path.stat().st_mtime <= _ttl()
    except OSError:
        return False


def store_preview(downloaded: Path, url: str) -> Path:
    """Move a just-downloaded preview into the cache (atomic rename: no half-written file is ever seen)."""
    dest = preview_file(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(downloaded, dest)
    return dest


def prune() -> int:
    """Delete cached previews older than FOOTAGE_CACHE_SECONDS (run once per job). Returns how many."""
    folder = Path(get_settings().FOOTAGE_CACHE_DIR) / "previews"
    removed, now, ttl = 0, time.time(), _ttl()
    if folder.is_dir():
        for path in folder.glob("*.jpg"):
            try:
                if now - path.stat().st_mtime > ttl:
                    path.unlink()
                    removed += 1
            except OSError:
                pass  # another worker removed or is using it
    with _table_lock:
        for key in [k for k, (t, _) in _search_results.items() if time.monotonic() - t > ttl]:
            del _search_results[key]
    return removed


def reset() -> None:
    """Forget the in-memory search results (tests)."""
    with _table_lock:
        _search_results.clear()
        _key_locks.clear()
