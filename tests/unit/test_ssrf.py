"""SSRF guard: never fetch private / loopback / link-local addresses for a user."""
import pytest

import app.pipeline.net as net
from app.config import get_settings
from app.errors import AppError
from app.pipeline import intake
from app.pipeline.providers.base import Candidate
from app.pipeline.providers.pixabay import PixabayVideoProvider
from app.security import BlockedURL, check_public_url
from tests.conftest import AUTH
from tests.fake_api import FakeAPI


@pytest.fixture
def guard_on(monkeypatch):
    monkeypatch.setenv("ALLOW_PRIVATE_URLS", "false")
    get_settings.cache_clear()


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/a.mp3", "http://localhost:9000/bucket", "http://10.0.0.5/a.mp3", "http://192.168.1.1/",
    "http://172.16.0.1/", "http://169.254.169.254/latest/meta-data/", "http://[::1]/a.mp3",
    "http://[::ffff:127.0.0.1]/a.mp3", "http://0.0.0.0/", "ftp://example.com/a.mp3", "file:///etc/passwd",
])
def test_private_and_odd_urls_are_blocked(guard_on, url):
    with pytest.raises(BlockedURL):
        check_public_url(url)


def test_public_ip_is_allowed(guard_on):
    check_public_url("https://8.8.8.8/file.mp3")  # an IP literal needs no DNS lookup


def test_audio_url_to_private_address_is_refused(guard_on, tmp_path):
    with pytest.raises(AppError) as e:
        intake.download_audio("http://127.0.0.1:9/voice.mp3", tmp_path / "x")
    assert e.value.code.value == "INVALID_INPUT"
    assert e.value.details == {"reason": "private_or_local_address"}


def test_redirect_to_a_private_address_is_refused(monkeypatch, tmp_path):
    """The first URL is fine, but it redirects somewhere private: every hop is checked."""
    server = FakeAPI(lambda *a: (200, b"never sent"))
    base_handler = server.server.RequestHandlerClass

    class Redirect(base_handler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "/private/secret")
            self.send_header("Content-Length", "0")
            self.end_headers()

    server.server.RequestHandlerClass = Redirect

    def fake_check(url):
        if "/private/" in url:
            raise BlockedURL("points to a private address")

    monkeypatch.setattr(net, "check_public_url", fake_check)
    try:
        with pytest.raises(net.DownloadError) as e:
            net.fetch_to_file(server.url + "/start.mp3", tmp_path / "out", 1024 * 1024, 5)
    finally:
        server.close()
    assert e.value.reason == "blocked"
    assert not (tmp_path / "out").exists()


def test_footage_download_to_private_address_is_refused(guard_on, tmp_path):
    cand = Candidate(provider="pixabay_video", id="1", kind="video", download_url="http://169.254.169.254/x.mp4")
    with pytest.raises(net.DownloadError) as e:
        PixabayVideoProvider().fetch(cand, tmp_path)
    assert e.value.reason == "blocked"


def test_api_rejects_private_audio_url_before_creating_a_job(guard_on, client):
    resp = client.post("/api/v1/broll/jobs", json={"audio_url": "http://169.254.169.254/latest/meta-data"},
                       headers=AUTH)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_INPUT"
    assert resp.json()["error"]["details"]["reason"] == "private_or_local_address"
