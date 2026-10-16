"""Footage providers: licence filter, parsing real-shaped answers, errors, on/off switches."""
import json

import pytest

from app.config import get_settings
from app.pipeline.providers import active_providers, get_provider, provider_status
from app.pipeline.providers.base import ClipNotUsable, ProviderError
from app.pipeline.providers.wikimedia import check_licence
from tests.fake_api import FakeAPI


def setenv(monkeypatch, **values):
    for key, value in values.items():
        monkeypatch.setenv(key, str(value))
    get_settings.cache_clear()


# ---------- Wikimedia licence filter ----------

@pytest.mark.parametrize("short_name, code, allowed, name", [
    ("CC BY 4.0", "cc-by-4.0", True, "CC BY 4.0"),
    ("CC BY 2.0", "", True, "CC BY 2.0"),
    ("CC0", "cc0", True, "CC0"),
    ("Public domain", "pd", True, "Public domain"),
    ("PD-USGov", "", True, "Public domain"),
    ("CC BY-SA 4.0", "cc-by-sa-4.0", False, None),
    ("CC BY-SA 3.0", "", False, None),
    ("CC BY-NC 2.0", "cc-by-nc-2.0", False, None),
    ("CC BY-NC-SA 4.0", "", False, None),
    ("CC BY-ND 3.0", "cc-by-nd-3.0", False, None),
    ("GFDL", "gfdl", False, None),
    ("", "", False, None),
    ("Some custom licence", "", False, None),
])
def test_wikimedia_licence_filter(short_name, code, allowed, name):
    ok, result = check_licence(short_name, code)
    assert ok is allowed
    if allowed:
        assert result == name
    else:
        assert result.startswith("rejected licence")


def wiki_page(pageid, licence, code, artist="<a href='x'>Jane Doe</a>", restrictions="", mime="image/jpeg"):
    return {"pageid": pageid, "title": f"File:Photo {pageid}.jpg", "imageinfo": [{
        "url": f"https://upload.example.org/{pageid}.jpg", "descriptionurl": f"https://commons.example.org/{pageid}",
        "thumburl": f"https://upload.example.org/thumb/{pageid}.jpg/500px-{pageid}.jpg",
        "width": 4000, "height": 3000, "mime": mime, "extmetadata": {
            "LicenseShortName": {"value": licence}, "License": {"value": code},
            "LicenseUrl": {"value": "https://creativecommons.org/licenses/by/4.0/" if "BY" in licence else ""},
            "Artist": {"value": artist}, "Categories": {"value": "Data centers|Servers"},
            "Restrictions": {"value": restrictions}}}]}


def test_wikimedia_keeps_only_allowed_licences_and_records_attribution(monkeypatch):
    pages = [wiki_page(1, "CC BY 4.0", "cc-by-4.0"), wiki_page(2, "CC BY-SA 4.0", "cc-by-sa-4.0"),
             wiki_page(3, "CC BY-NC 2.0", "cc-by-nc-2.0"), wiki_page(4, "Public domain", "pd", artist=""),
             wiki_page(5, "GFDL", "gfdl"), wiki_page(6, "CC0", "cc0", restrictions="personality"),
             wiki_page(7, "CC BY 4.0", "cc-by-4.0", artist=""), wiki_page(8, "CC0", "cc0", mime="image/svg+xml")]
    server = FakeAPI(lambda *a: (200, {"query": {"pages": pages}}))
    try:
        setenv(monkeypatch, WIKIMEDIA_API_URL=server.url + "/w/api.php", WIKIMEDIA_USER_AGENT="tests/1.0 (me)")
        found = get_provider("wikimedia").search("server room", "landscape", 4.0)
    finally:
        server.close()
    assert [c.id for c in found] == ["1", "4"]  # BY-SA, NC, GFDL, personality, no-author CC BY, SVG all rejected
    by = found[0]
    assert by.licence_name == "CC BY 4.0" and by.licence_url.startswith("https://creativecommons.org/")
    assert by.attribution == "Jane Doe, via Wikimedia Commons, CC BY 4.0"
    assert by.download_url.endswith("/1920px-1.jpg") and (by.width, by.height) == (1920, 1440)
    assert by.kind == "image" and by.commercial_use and not by.watermark
    assert found[1].licence_url == "https://creativecommons.org/publicdomain/mark/1.0/"
    call = server.calls[0]
    assert call["headers"]["User-Agent"] == "tests/1.0 (me)"
    assert call["query"]["gsrsearch"] == "server room filetype:bitmap"


# ---------- Pixabay ----------

def pixabay_video_hit(vid, duration, with_medium=True):
    videos = {"small": {"url": f"https://cdn.example/{vid}_small.mp4", "width": 1280, "height": 720,
                        "size": 1000, "thumbnail": f"https://cdn.example/{vid}.jpg"}}
    if with_medium:
        videos["medium"] = {"url": f"https://cdn.example/{vid}_medium.mp4", "width": 1920, "height": 1080,
                            "size": 5000, "thumbnail": f"https://cdn.example/{vid}_m.jpg"}
    return {"id": vid, "pageURL": f"https://pixabay.com/videos/{vid}/", "tags": "server, data, technology",
            "duration": duration, "user": "PixUser", "videos": videos}


def test_pixabay_video_parses_hits_and_prefers_full_hd(monkeypatch):
    hits = [pixabay_video_hit(11, 12), pixabay_video_hit(12, 1), pixabay_video_hit(13, 9, with_medium=False)]
    server = FakeAPI(lambda *a: (200, {"total": 3, "hits": hits}))
    try:
        setenv(monkeypatch, PIXABAY_API_KEY="secret-key", PIXABAY_VIDEO_API_URL=server.url + "/api/videos/")
        found = get_provider("pixabay_video").search("data center", "landscape", 5.0)
    finally:
        server.close()
    assert [c.id for c in found] == ["11", "13"]  # the 1 s clip is too short for a 5 s scene
    first = found[0]
    assert first.download_url.endswith("_medium.mp4") and (first.width, first.height) == (1920, 1080)
    assert found[1].download_url.endswith("_small.mp4")
    assert first.licence_name == "Pixabay Content License" and first.licence_url.startswith("https://pixabay.com")
    assert first.tags == ["server", "data", "technology"] and first.attribution == "PixUser on Pixabay"
    assert server.calls[0]["query"]["key"] == "secret-key" and server.calls[0]["query"]["safesearch"] == "true"


def test_pixabay_image_parses_hits(monkeypatch):
    hit = {"id": 21, "pageURL": "https://pixabay.com/photos/21/", "tags": "coins, jar", "user": "Ann",
           "webformatURL": "https://cdn.example/21_640.jpg", "largeImageURL": "https://cdn.example/21_1280.jpg",
           "imageWidth": 6000, "imageHeight": 4000}
    server = FakeAPI(lambda *a: (200, {"hits": [hit]}))
    try:
        setenv(monkeypatch, PIXABAY_API_KEY="k", PIXABAY_IMAGE_API_URL=server.url + "/api/")
        found = get_provider("pixabay_image_motion").search("coins jar", "portrait", 5.0)
    finally:
        server.close()
    assert len(found) == 1 and found[0].kind == "image" and (found[0].width, found[0].height) == (1280, 853)
    assert server.calls[0]["query"]["orientation"] == "vertical"


def test_provider_http_error_and_timeout_become_provider_errors(monkeypatch):
    broken = FakeAPI(lambda *a: (503, {"error": "down"}))
    slow = FakeAPI(lambda *a: (200, {"hits": []}), delay=1.5)
    try:
        setenv(monkeypatch, PIXABAY_API_KEY="k", PIXABAY_VIDEO_API_URL=broken.url, PROVIDER_TIMEOUT_SECONDS="0.5")
        with pytest.raises(ProviderError, match="HTTP 503"):
            get_provider("pixabay_video").search("x", "landscape", 3)
        setenv(monkeypatch, PIXABAY_VIDEO_API_URL=slow.url)
        with pytest.raises(ProviderError, match="timed out"):
            get_provider("pixabay_video").search("x", "landscape", 3)
    finally:
        broken.close()
        slow.close()


def test_provider_error_message_never_contains_the_api_key(monkeypatch):
    server = FakeAPI(lambda *a: (500, b"oops"))
    try:
        setenv(monkeypatch, PIXABAY_API_KEY="super-secret-123", PIXABAY_VIDEO_API_URL=server.url)
        with pytest.raises(ProviderError) as e:
            get_provider("pixabay_video").search("x", "landscape", 3)
    finally:
        server.close()
    assert "super-secret-123" not in str(e.value)


# ---------- Switching providers on and off ----------

def test_providers_without_keys_are_skipped(monkeypatch):
    setenv(monkeypatch, PROVIDER_ORDER="pexels,pixabay_video,wikimedia,generated", PEXELS_API_KEY="",
           PIXABAY_API_KEY="", WIKIMEDIA_USER_AGENT="tests/1.0 (https://example.org/tests)")
    assert provider_status("pexels") == "PEXELS_API_KEY is empty"
    assert provider_status("pixabay_video") == "PIXABAY_API_KEY is empty"
    assert active_providers() == ["wikimedia", "generated"]
    setenv(monkeypatch, PIXABAY_API_KEY="k")
    assert active_providers() == ["pixabay_video", "wikimedia", "generated"]


def test_providers_disabled_env_and_override_file(monkeypatch, tmp_path):
    override = tmp_path / "override.json"
    setenv(monkeypatch, PROVIDER_ORDER="mock,wikimedia,generated", PROVIDERS_DISABLED="wikimedia",
           PROVIDERS_OVERRIDE_FILE=str(override), WIKIMEDIA_USER_AGENT="tests/1.0 (tests@example.org)")
    assert active_providers() == ["mock", "generated"]
    override.write_text(json.dumps({"disabled": ["mock"]}), encoding="utf-8")
    assert active_providers() == ["generated"]  # read again on every call: works mid-job
    assert provider_status("mock").startswith("switched off by")
    override.write_text("{ not json", encoding="utf-8")
    assert active_providers() == ["mock", "generated"]  # a broken file switches nothing off


def test_wikimedia_without_a_contact_in_the_user_agent_is_skipped(monkeypatch):
    setenv(monkeypatch, WIKIMEDIA_USER_AGENT="broll-engine/0.3 (no contact here)")
    assert provider_status("wikimedia") == "WIKIMEDIA_USER_AGENT has no contact (add your email or a URL)"
    setenv(monkeypatch, WIKIMEDIA_USER_AGENT="broll-engine/0.3 (https://github.com/me/broll-engine)")
    assert provider_status("wikimedia") is None


# ---------- lookup(clip_id): used by the clip swap with provider + clip_id ----------

def test_pixabay_video_lookup_by_id(monkeypatch):
    server = FakeAPI(lambda m, p, q, b: (200, {"hits": [pixabay_video_hit(int(q["id"]), 3)] if q["id"] == "11" else []}))
    try:
        setenv(monkeypatch, PIXABAY_API_KEY="k", PIXABAY_VIDEO_API_URL=server.url + "/api/videos/")
        found = get_provider("pixabay_video").lookup("11")
        with pytest.raises(ClipNotUsable, match="not found"):
            get_provider("pixabay_video").lookup("99")
        with pytest.raises(ClipNotUsable, match="numbers"):
            get_provider("pixabay_video").lookup("abc")
    finally:
        server.close()
    assert found.uid == "pixabay_video:11" and found.licence_name == "Pixabay Content License"
    assert server.calls[0]["query"]["id"] == "11" and "q" not in server.calls[0]["query"]


def test_wikimedia_lookup_applies_the_same_licence_filter(monkeypatch):
    pages = {"Good.jpg": [wiki_page(1, "CC0", "cc0")], "Shared.jpg": [wiki_page(2, "CC BY-SA 4.0", "cc-by-sa-4.0")]}
    server = FakeAPI(lambda m, p, q, b: (200, {"query": {"pages": pages.get(q.get("titles", "").removeprefix("File:"),
                                                                            [])}}))
    try:
        setenv(monkeypatch, WIKIMEDIA_API_URL=server.url + "/w/api.php", WIKIMEDIA_USER_AGENT="tests/1.0 (me)")
        provider = get_provider("wikimedia")
        assert provider.lookup("File:Good.jpg").licence_name == "CC0"
        with pytest.raises(ClipNotUsable, match="share-alike"):
            provider.lookup("Shared.jpg")
        with pytest.raises(ClipNotUsable, match="not found"):
            provider.lookup("Missing.jpg")
    finally:
        server.close()
    assert server.calls[0]["query"]["titles"] == "File:Good.jpg"


def test_sources_without_lookup_say_so():
    with pytest.raises(ClipNotUsable):
        get_provider("generated").lookup("card-001")
