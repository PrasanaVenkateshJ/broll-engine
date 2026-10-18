"""Openverse (api.openverse.org, no key). Openly licensed photos, turned into motion clips by render.py
(Ken Burns), like Wikimedia. A last-resort photo source after Pixabay and Wikimedia.

Licence filter (check_licence), the same rule as Wikimedia:
- accepted: CC0, Public Domain Mark ("pdm") and CC BY (any version) -> safe for commercial use,
- rejected: everything else (CC BY-SA, anything NC or ND, "sampling+", unknown).
We ask the API for license=cc0,pdm,by AND check every result again ourselves (never trust one filter).
CC BY needs credit: a result without a creator is rejected; the attribution names title, creator, licence
and its URL. Mature results are rejected.

Quotas (anonymous, read from the API's own headers on 2026-10-17): 20 searches per minute, 200 per day,
page_size at most 20; thumbnails have their own 1,000 per day. So: PROVIDER_MAX_QUERIES caps it to 2
queries per scene, PROVIDER_REQUESTS_PER_MINUTE to 18 per minute, and a 429 makes it sit out for a while.
"""
from app.config import get_settings
from app.pipeline.providers.base import Candidate, ClipNotUsable, Provider, ProviderError, get_json

ALLOWED = {"cc0": "CC0", "pdm": "Public Domain Mark", "by": "CC BY"}
DEFAULT_URLS = {"cc0": "https://creativecommons.org/publicdomain/zero/1.0/",
                "pdm": "https://creativecommons.org/publicdomain/mark/1.0/"}
API_MAX_PAGE_SIZE = 20  # anonymous requests


def check_licence(code: str, version: str = "") -> tuple[bool, str]:
    """(allowed?, licence name or the reason it was rejected). Exact codes only: "by-sa" is not "by"."""
    code = (code or "").strip().lower()
    if code not in ALLOWED:
        return False, f"rejected licence '{code or 'unknown'}' (only CC0, Public Domain Mark or CC BY)"
    name = ALLOWED[code]
    if code == "by" and version:
        name = f"CC BY {version}"
    return True, name


def parse_results(data: dict, query: str) -> tuple[list[Candidate], list[str]]:
    """The API answer -> (accepted candidates, rejection reasons)."""
    results = data.get("results")
    if not isinstance(results, list):
        raise ProviderError("unexpected answer (no results)")
    out, rejected = [], []
    for item in results:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "untitled").strip()[:120]
        ok, licence = check_licence(str(item.get("license") or ""), str(item.get("license_version") or ""))
        if not ok:
            rejected.append(f"{title}: {licence}")
            continue
        if item.get("mature"):
            rejected.append(f"{title}: marked mature")
            continue
        creator = str(item.get("creator") or "").strip()
        code = str(item.get("license")).lower()
        licence_url = str(item.get("license_url") or "") or DEFAULT_URLS.get(code, "")
        if code == "by" and not creator:
            rejected.append(f"{title}: CC BY without a creator to credit")
            continue
        if not licence_url.startswith(("https://creativecommons.org/", "http://creativecommons.org/")):
            rejected.append(f"{title}: licence URL is not a Creative Commons address ({licence_url or 'none'})")
            continue
        if not item.get("url") or not item.get("thumbnail") or not item.get("id"):
            continue
        tags = [str(t.get("name", "")).strip().lower() for t in item.get("tags") or [] if isinstance(t, dict)]
        attribution = f'"{title}" by {creator or "unknown creator"}, {licence} ({licence_url}), via Openverse'
        if item.get("source"):
            attribution += f" / {item['source']}"
        out.append(Candidate(
            provider="openverse", id=str(item["id"]), kind="image", preview_url=str(item["thumbnail"]),
            download_url=str(item["url"]), width=int(item.get("width") or 0), height=int(item.get("height") or 0),
            tags=[t for t in tags if t], title=title, licence_name=licence, licence_url=licence_url,
            attribution=attribution, page_url=str(item.get("foreign_landing_url") or ""), query=query,
            extra={"source": item.get("source"), "creator": creator}))
    return out, rejected


class OpenverseProvider(Provider):
    name = "openverse"
    media_kind = "image"
    rate_key = "openverse"
    api_info = "Openverse API (api.openverse.org/v1/images, anonymous) + FFmpeg Ken Burns motion"

    def headers(self) -> dict:
        # Openverse asks API users to identify themselves; we reuse the Wikimedia contact User-Agent.
        return {"User-Agent": get_settings().WIKIMEDIA_USER_AGENT}

    def download_headers(self):
        return self.headers()

    def search(self, query, orientation, min_duration):
        s = get_settings()
        data = get_json(s.OPENVERSE_API_URL, {
            "q": query[:200], "license": "cc0,pdm,by", "page_size": str(self.results_per_request(API_MAX_PAGE_SIZE)),
            "mature": "false", "filter_dead": "true",
            **({"aspect_ratio": {"landscape": "wide", "portrait": "tall"}[orientation]}
               if orientation in ("landscape", "portrait") else {})},
            headers=self.headers(), rate_key=self.rate_key)
        candidates, _rejected = parse_results(data, query)
        return candidates

    def lookup(self, clip_id):
        """One image by its Openverse id (clip swap). The same licence filter as search."""
        s = get_settings()
        if not clip_id or any(ch not in "0123456789abcdef-" for ch in clip_id.lower()):
            raise ClipNotUsable(f"Openverse ids look like 8653eca8-08b0-..., got '{clip_id}'")
        data = get_json(f"{s.OPENVERSE_API_URL.rstrip('/')}/{clip_id}/", {}, headers=self.headers(),
                        rate_key=self.rate_key)
        candidates, rejected = parse_results({"results": [data]}, f"openverse image {clip_id}")
        if candidates:
            return candidates[0]
        raise ClipNotUsable(rejected[0] if rejected else f"Openverse image {clip_id} not found")
