"""What every footage provider has in common.

A provider answers ONE question: search(query, orientation, min_duration) -> [Candidate].
A Candidate is a possible clip with everything we must record about it (where it came
from, its licence, who made it). Downloading the chosen one is fetch().

Errors: a provider raises ProviderError (timeout, HTTP error, bad answer). The caller
(rank.py) turns that into a job WARNING and tries the next provider; it never fails the job.
"""
import logging
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.config import get_settings
from app.pipeline import media, net
from app.security import BlockedURL, check_public_url

log = logging.getLogger("app.pipeline.providers")

ORIENTATIONS = ("landscape", "portrait", "square")


# ---------- API politeness: a per-minute request limit + a cooldown after HTTP 429 ----------

_limit_lock = threading.Lock()
_sent: dict[str, deque] = {}          # rate key -> times of the requests in the last 60 s
_cooldown_until: dict[str, float] = {}  # rate key -> time before which we do not ask again (after a 429)
MAX_LIMIT_WAIT_SECONDS = 65.0


def reset_limits() -> None:
    """Forget all request times and cooldowns (tests)."""
    with _limit_lock:
        _sent.clear()
        _cooldown_until.clear()


def cooling_down(rate_key: str | None) -> float:
    """Seconds left before this API may be asked again after a 429 (0 = go ahead)."""
    if not rate_key:
        return 0.0
    with _limit_lock:
        return max(0.0, _cooldown_until.get(rate_key, 0.0) - time.monotonic())


def _wait_for_slot(rate_key: str) -> None:
    """Block until one more request fits in PROVIDER_REQUESTS_PER_MINUTE for this key (a sliding 60 s window).
    The whole job shares the limit (all footage threads), so a long job waits instead of getting HTTP 429."""
    per_minute = get_settings().provider_requests_per_minute.get(rate_key)
    if not per_minute:
        return
    waited = 0.0
    while True:
        with _limit_lock:
            now = time.monotonic()
            sent = _sent.setdefault(rate_key, deque())
            while sent and now - sent[0] >= 60.0:
                sent.popleft()
            if len(sent) < per_minute:
                sent.append(now)
                return
            pause = 60.0 - (now - sent[0]) + 0.05
        if waited + pause > MAX_LIMIT_WAIT_SECONDS:
            raise ProviderError(f"client request limit ({per_minute:g} per minute) reached")
        log.info("request limit for %s: waiting %.1f s", rate_key, pause)
        time.sleep(pause)
        waited += pause


def _start_cooldown(rate_key: str, retry_after: str | None) -> float:
    try:
        seconds = float(retry_after) if retry_after is not None else 0.0
    except ValueError:
        seconds = 0.0
    seconds = max(seconds, get_settings().PROVIDER_COOLDOWN_SECONDS)
    with _limit_lock:
        _cooldown_until[rate_key] = time.monotonic() + seconds
    return seconds


def safe_name(text: str) -> str:
    """A clip id that is safe as part of a file name."""
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in text)[:60]


class ProviderError(Exception):
    """The provider could not answer (it is down, slow, or said no)."""


class ClipNotUsable(ProviderError):
    """lookup(): the provider answered, but this clip does not exist or its licence is not allowed."""


@dataclass
class Candidate:
    provider: str
    id: str                      # the provider's own clip id
    kind: str                    # "video" | "image" | "generated"
    preview_url: str = ""        # small picture we score with CLIP
    download_url: str = ""
    duration: float | None = None  # seconds; None for images (any length) and cards
    width: int = 0
    height: int = 0
    tags: list[str] = field(default_factory=list)
    title: str = ""
    licence_name: str = ""
    licence_url: str = ""
    attribution: str = ""
    page_url: str = ""
    commercial_use: bool = True
    watermark: bool = False
    ai_generated: bool = False
    generated: bool = False
    query: str = ""              # which search query found it
    extra: dict = field(default_factory=dict)

    @property
    def uid(self) -> str:
        """Unique across providers, e.g. "pixabay_video:12345". Used for the no-repeat rule."""
        return f"{self.provider}:{self.id}"

    @property
    def orientation(self) -> str:
        if not self.width or not self.height:
            return "any"
        ratio = self.width / self.height
        return "landscape" if ratio > 1.1 else "portrait" if ratio < 0.9 else "square"

    def search_text(self) -> str:
        """Everything that describes the clip, for the exclude-terms check."""
        return " ".join([self.title, *self.tags, str(self.extra.get("description", ""))])

    def to_dict(self) -> dict:
        return asdict(self)


class Provider:
    name = "base"
    key_setting: str | None = None   # e.g. "PIXABAY_API_KEY": no key -> provider is skipped
    api_info = ""                    # what we call, for manifest.json
    # May search results + previews be reused by LATER jobs (FOOTAGE_CACHE_SECONDS)? True for real APIs;
    # False for test sources whose answers change on purpose between tests.
    cache_across_jobs = True
    media_kind = "video"             # "video" | "image" (photo-motion) | "generated": for PREFER_VIDEO_MARGIN
    rate_key: str | None = None      # shared request limit + 429 cooldown (PROVIDER_REQUESTS_PER_MINUTE)

    def not_ready(self) -> str | None:
        """None if the provider has what it needs (e.g. its API key), else the reason it cannot run."""
        if self.key_setting and not getattr(get_settings(), self.key_setting):
            return f"{self.key_setting} is empty"
        left = cooling_down(self.rate_key)
        if left:
            return f"rate limited (HTTP 429), cooling down for {left:.0f} s more"
        return None

    def max_queries(self, queries: list[str]) -> list[str]:
        """The queries this source is asked (PROVIDER_MAX_QUERIES caps APIs with small free quotas)."""
        cap = get_settings().provider_max_queries.get(self.name)
        return queries[:cap] if cap else queries

    def results_per_request(self, api_max: int) -> int:
        """PROVIDER_RESULTS_PER_QUERY x PROVIDER_PAGES, fetched as ONE request (capped at the API's maximum)."""
        s = get_settings()
        return max(1, min(api_max, s.PROVIDER_RESULTS_PER_QUERY * max(1, s.PROVIDER_PAGES)))

    def probe_frames(self, candidate: "Candidate", dest_dir: Path, positions: list[float]) -> list[Path]:
        """Extra frames of a VIDEO candidate for scoring, at `positions` (0..1) of its length, from its small
        rendition (candidate.extra["probe_url"], ~1 MB). [] when there is no cheap way to get them."""
        s = get_settings()
        url = candidate.extra.get("probe_url")
        if candidate.kind != "video" or not url or not positions:
            return []
        dest_dir.mkdir(parents=True, exist_ok=True)
        clip = net.fetch_to_file(url, dest_dir / f"{self.name}_{safe_name(candidate.id)}_probe.mp4",
                                 s.MULTI_FRAME_MAX_MB * net.MB, s.CLIP_DOWNLOAD_TIMEOUT_SECONDS,
                                 headers=self.download_headers())
        duration = candidate.duration or media.duration_of(clip)
        frames = []
        for i, pos in enumerate(positions):
            out = dest_dir / f"{self.name}_{safe_name(candidate.id)}_p{i}.jpg"
            media.ffmpeg("-ss", f"{max(0.0, duration * pos):.2f}", "-i", str(clip), "-frames:v", "1",
                         "-vf", "scale=320:-2", str(out))
            if out.is_file():
                frames.append(out)
        clip.unlink(missing_ok=True)
        return frames

    def search(self, query: str, orientation: str, min_duration: float) -> list[Candidate]:
        raise NotImplementedError

    def lookup(self, clip_id: str) -> Candidate:
        """ONE clip by the provider's own id (clip swap with provider + clip_id). ProviderError if unknown."""
        raise ClipNotUsable(f"{self.name} cannot look up a clip by its id")

    def fetch(self, candidate: Candidate, dest_dir: Path) -> Path:
        """Download the full clip / image. Raises net.DownloadError on any problem."""
        s = get_settings()
        suffix = Path(urlsplit(candidate.download_url).path).suffix.lower()
        suffix = suffix if suffix in (".mp4", ".webm", ".mov", ".jpg", ".jpeg", ".png", ".webp") else (
            ".mp4" if candidate.kind == "video" else ".jpg")
        dest = dest_dir / f"{self.name}_{safe_name(candidate.id)}{suffix}"
        return net.fetch_to_file(candidate.download_url, dest, s.MAX_CLIP_DOWNLOAD_MB * net.MB,
                                 s.CLIP_DOWNLOAD_TIMEOUT_SECONDS, headers=self.download_headers())

    def fetch_preview(self, candidate: Candidate, dest_dir: Path) -> Path:
        """Download the small preview picture that CLIP scores (PREVIEW_TIMEOUT_SECONDS at most)."""
        s = get_settings()
        dest = dest_dir / f"{self.name}_{safe_name(candidate.id)}.jpg"
        return net.fetch_to_file(candidate.preview_url, dest, 10 * net.MB, s.PREVIEW_TIMEOUT_SECONDS,
                                 headers=self.download_headers())

    def download_headers(self) -> dict:
        return {}

    def info(self) -> dict:
        return {"name": self.name, "api": self.api_info}


def get_json(url: str, params: dict, headers: dict | None = None, rate_key: str | None = None) -> dict:
    """GET a JSON API with PROVIDER_TIMEOUT_SECONDS. Every problem becomes ProviderError.
    rate_key: the API's shared request limit (waits for a free slot first) and 429 cooldown."""
    s = get_settings()
    if rate_key:
        if left := cooling_down(rate_key):
            raise ProviderError(f"rate limited (HTTP 429), cooling down for {left:.0f} s more")
        _wait_for_slot(rate_key)
    try:
        check_public_url(url)
        resp = net.shared_client().get(url, params=params, headers=headers, timeout=s.PROVIDER_TIMEOUT_SECONDS)
    except BlockedURL as e:
        raise ProviderError(f"blocked URL: {e}") from None
    except httpx.TimeoutException:
        raise ProviderError(f"timed out after {s.PROVIDER_TIMEOUT_SECONDS:g} s") from None
    except httpx.HTTPError as e:
        raise ProviderError(f"network error ({type(e).__name__})") from None
    if resp.status_code == 429 and rate_key:
        seconds = _start_cooldown(rate_key, resp.headers.get("retry-after"))
        raise ProviderError(f"HTTP 429 (rate limited; skipped for {seconds:.0f} s)")
    if resp.status_code != 200:
        raise ProviderError(f"HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError:
        raise ProviderError("the answer was not JSON") from None
