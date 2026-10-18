"""Wikimedia Commons (no key). Still images, turned into motion clips by render.py (Ken Burns).

Wikimedia files have MANY licences, so each file is checked with check_licence():
- accepted: CC0, Public Domain, CC BY (any version)   -> safe for commercial use
- rejected: CC BY-SA (share-alike), anything NC (non-commercial) or ND (no-derivatives),
            GFDL-only, and anything unknown.
CC BY needs credit, so we record the author + licence URL as the attribution.
Wikimedia requires a User-Agent that says who we are WITH a contact (email or URL), e.g.
"broll-engine/0.3 (https://github.com/you/broll-engine; you@example.com)". Without one it
answers HTTP 403, so the provider counts as "not ready" and is skipped.
"""
import html
import re

from app.config import get_settings
from app.pipeline.providers.base import Candidate, ClipNotUsable, Provider, ProviderError, get_json

METADATA_FIELDS = ("LicenseShortName|License|LicenseUrl|Artist|Credit|ImageDescription|ObjectName|Categories|"
                   "Restrictions")
PREVIEW_WIDTH = 500    # Wikimedia's standard thumbnail sizes include 500 and 1920
DOWNLOAD_WIDTH = 1920
DEFAULT_URLS = {"CC0": "https://creativecommons.org/publicdomain/zero/1.0/",
                "Public domain": "https://creativecommons.org/publicdomain/mark/1.0/"}


def check_licence(short_name: str, code: str = "") -> tuple[bool, str]:
    """(allowed?, normalised licence name or the reason it was rejected)."""
    forms = [re.sub(r"[\s_]+", "-", (raw or "").strip().lower()) for raw in (code, short_name)]
    forms = [f for f in forms if f]
    for form in forms:
        parts = set(re.split(r"[-,/;()]+", form))
        if parts & {"sa", "nc", "nd"}:
            return False, f"rejected licence '{short_name or code}' (share-alike, non-commercial or no-derivatives)"
    for form in forms:
        if form.startswith(("cc0", "cc-zero")):
            return True, "CC0"
        if form.startswith("pd") or "public-domain" in form:
            return True, "Public domain"
        if m := re.fullmatch(r"cc-by(?:-(\d(?:\.\d)?))?(?:[-,;].*)?", form):
            return True, f"CC BY {m.group(1)}" if m.group(1) else "CC BY"
    return False, f"rejected licence '{short_name or code or 'unknown'}' (not CC0, public domain or CC BY)"


def _plain(value: str) -> str:
    """Wikimedia metadata is HTML: '<a href=..>Jane</a>' -> 'Jane'."""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value or ""))).strip()


def parse_pages(data: dict, query: str) -> tuple[list[Candidate], list[str]]:
    """Turn the API answer into candidates. Returns (accepted, rejection reasons)."""
    pages = (data.get("query") or {}).get("pages")
    if pages is None:
        return [], []
    if isinstance(pages, dict):  # formatversion=1 shape
        pages = list(pages.values())
    out, rejected = [], []
    for page in pages:
        info = (page.get("imageinfo") or [{}])[0]
        meta = {k: (v or {}).get("value", "") if isinstance(v, dict) else v
                for k, v in (info.get("extmetadata") or {}).items()}
        if info.get("mime") not in ("image/jpeg", "image/png") or not info.get("thumburl"):
            continue
        ok, licence = check_licence(str(meta.get("LicenseShortName", "")), str(meta.get("License", "")))
        if not ok:
            rejected.append(f"{page.get('title', '?')}: {licence}")
            continue
        restrictions = str(meta.get("Restrictions", "")).lower()
        if "personality" in restrictions or "trademark" in restrictions:
            rejected.append(f"{page.get('title', '?')}: has personality/trademark restrictions")
            continue
        author = _plain(str(meta.get("Artist", ""))) or _plain(str(meta.get("Credit", "")))
        if licence.startswith("CC BY") and not author:
            rejected.append(f"{page.get('title', '?')}: CC BY without an author to credit")
            continue
        width, height = int(info.get("width") or 0), int(info.get("height") or 0)
        thumb = info["thumburl"]
        download = thumb.replace(f"/{PREVIEW_WIDTH}px-", f"/{DOWNLOAD_WIDTH}px-") if width > DOWNLOAD_WIDTH \
            else info.get("url", thumb)
        if width > DOWNLOAD_WIDTH:
            height, width = round(height * DOWNLOAD_WIDTH / width), DOWNLOAD_WIDTH
        title = str(page.get("title", "")).removeprefix("File:")
        licence_url = str(meta.get("LicenseUrl", "")) or DEFAULT_URLS.get(licence, "")
        out.append(Candidate(
            provider="wikimedia", id=str(page.get("pageid") or title), kind="image", preview_url=thumb,
            download_url=download, width=width, height=height, title=_plain(str(meta.get("ObjectName", ""))) or title,
            tags=[c.strip().lower() for c in str(meta.get("Categories", "")).split("|") if c.strip()],
            licence_name=licence, licence_url=licence_url,
            attribution=f"{author or 'Unknown author'}, via Wikimedia Commons, {licence}",
            page_url=info.get("descriptionurl", ""), query=query,
            extra={"description": _plain(str(meta.get("ImageDescription", "")))[:300]}))
    return out, rejected


class WikimediaProvider(Provider):
    name = "wikimedia"
    media_kind = "image"
    api_info = "Wikimedia Commons API (commons.wikimedia.org/w/api.php) + FFmpeg Ken Burns motion"

    def not_ready(self):
        # Wikimedia answers 403 to clients whose User-Agent has no way to contact the owner.
        agent = get_settings().WIKIMEDIA_USER_AGENT
        if "@" not in agent and "http" not in agent:
            return "WIKIMEDIA_USER_AGENT has no contact (add your email or a URL)"
        return None

    def download_headers(self):
        return {"User-Agent": get_settings().WIKIMEDIA_USER_AGENT}

    def search(self, query, orientation, min_duration):
        s = get_settings()
        data = get_json(s.WIKIMEDIA_API_URL, {
            "action": "query", "format": "json", "formatversion": "2", "generator": "search",
            "gsrsearch": f"{query} filetype:bitmap", "gsrnamespace": "6",
            "gsrlimit": str(self.results_per_request(50)), "prop": "imageinfo",
            "iiprop": "url|size|mime|extmetadata", "iiurlwidth": str(PREVIEW_WIDTH),
            "iiextmetadatafilter": METADATA_FIELDS}, headers=self.download_headers())
        if "error" in data:
            raise ProviderError(f"API error {data['error'].get('code', '?')}")
        candidates, _rejected = parse_pages(data, query)
        return candidates

    def lookup(self, clip_id):
        """clip_id = the page id (digits) or the file name ("Data_center.jpg" / "File:Data_center.jpg").
        The SAME licence filter as search: a CC BY-SA / NC / ND / unknown file is refused."""
        s = get_settings()
        params = {"action": "query", "format": "json", "formatversion": "2", "prop": "imageinfo",
                  "iiprop": "url|size|mime|extmetadata", "iiurlwidth": str(PREVIEW_WIDTH),
                  "iiextmetadatafilter": METADATA_FIELDS}
        if clip_id.isdigit():
            params["pageids"] = clip_id
        else:
            params["titles"] = clip_id if clip_id.startswith("File:") else f"File:{clip_id}"
        data = get_json(s.WIKIMEDIA_API_URL, params, headers=self.download_headers())
        if "error" in data:
            raise ProviderError(f"API error {data['error'].get('code', '?')}")
        candidates, rejected = parse_pages(data, f"wikimedia file {clip_id}")
        if candidates:
            return candidates[0]
        raise ClipNotUsable(rejected[0] if rejected else f"Wikimedia file {clip_id} not found (or not a photo)")
