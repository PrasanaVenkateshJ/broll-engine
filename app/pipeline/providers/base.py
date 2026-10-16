"""What every footage provider has in common.

A provider answers ONE question: search(query, orientation, min_duration) -> [Candidate].
A Candidate is a possible clip with everything we must record about it (where it came
from, its licence, who made it). Downloading the chosen one is fetch().

Errors: a provider raises ProviderError (timeout, HTTP error, bad answer). The caller
(rank.py) turns that into a job WARNING and tries the next provider; it never fails the job.
"""
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.config import get_settings
from app.pipeline import net
from app.security import BlockedURL, check_public_url

ORIENTATIONS = ("landscape", "portrait", "square")


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

    def not_ready(self) -> str | None:
        """None if the provider has what it needs (e.g. its API key), else the reason it cannot run."""
        if self.key_setting and not getattr(get_settings(), self.key_setting):
            return f"{self.key_setting} is empty"
        return None

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


def get_json(url: str, params: dict, headers: dict | None = None) -> dict:
    """GET a JSON API with PROVIDER_TIMEOUT_SECONDS. Every problem becomes ProviderError."""
    s = get_settings()
    try:
        check_public_url(url)
        resp = net.shared_client().get(url, params=params, headers=headers, timeout=s.PROVIDER_TIMEOUT_SECONDS)
    except BlockedURL as e:
        raise ProviderError(f"blocked URL: {e}") from None
    except httpx.TimeoutException:
        raise ProviderError(f"timed out after {s.PROVIDER_TIMEOUT_SECONDS:g} s") from None
    except httpx.HTTPError as e:
        raise ProviderError(f"network error ({type(e).__name__})") from None
    if resp.status_code != 200:
        raise ProviderError(f"HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError:
        raise ProviderError("the answer was not JSON") from None
