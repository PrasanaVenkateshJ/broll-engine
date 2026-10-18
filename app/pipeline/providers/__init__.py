"""Footage providers: which ones exist, and which ones are switched on RIGHT NOW.

A provider is used only if ALL of these are true:
1. it is in PROVIDER_ORDER (that is also the order we try them in),
2. it is not in PROVIDERS_DISABLED,
3. it is not in the "disabled" list of PROVIDERS_OVERRIDE_FILE - this small JSON file is
   read again every time we ask, so you can switch a provider off DURING a job:
       {"disabled": ["pixabay_video"]}
4. it has what it needs: its API key (pexels without PEXELS_API_KEY is skipped), or for
   wikimedia a User-Agent with a contact,
5. it is not cooling down after an HTTP 429 (rate limit) answer.
"""
import json
import logging
from pathlib import Path

from app.config import get_settings
from app.pipeline.providers.base import Candidate, ClipNotUsable, Provider, ProviderError
from app.pipeline.providers.generated import GeneratedProvider
from app.pipeline.providers.mock import MockProvider
from app.pipeline.providers.openverse import OpenverseProvider
from app.pipeline.providers.pexels import PexelsProvider
from app.pipeline.providers.pixabay import PixabayImageProvider, PixabayVideoProvider
from app.pipeline.providers.wikimedia import WikimediaProvider

log = logging.getLogger("app.pipeline.providers")

REGISTRY: dict[str, type[Provider]] = {
    p.name: p for p in (PixabayVideoProvider, PixabayImageProvider, WikimediaProvider, OpenverseProvider,
                        PexelsProvider, GeneratedProvider, MockProvider)
}

__all__ = ["Candidate", "ClipNotUsable", "Provider", "ProviderError", "REGISTRY", "get_provider", "override_disabled",
           "provider_status", "active_providers"]


def get_provider(name: str) -> Provider:
    return REGISTRY[name]()


def override_disabled() -> set[str]:
    """Names listed in the override file. A missing or broken file switches nothing off."""
    path = Path(get_settings().PROVIDERS_OVERRIDE_FILE)
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(n).strip().lower() for n in data.get("disabled", [])}
    except (OSError, ValueError, AttributeError) as e:
        log.warning("ignoring unreadable providers override file %s: %s", path, e)
        return set()


def provider_status(name: str) -> str | None:
    """None if the provider can be used now, else the reason it is off."""
    s = get_settings()
    if name not in REGISTRY:
        return "unknown provider name"
    if name in s.providers_disabled:
        return "switched off by PROVIDERS_DISABLED"
    if name in override_disabled():
        return f"switched off by {s.PROVIDERS_OVERRIDE_FILE}"
    return REGISTRY[name]().not_ready()


def active_providers() -> list[str]:
    return [name for name in get_settings().provider_order if provider_status(name) is None]
