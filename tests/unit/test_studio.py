"""The B-roll Studio web UI: GET / serves the page, /studio/<file> serves its static files (safely),
and the two small API routes the Studio needs: GET /api/v1/jobs and GET /api/v1/config/public."""
import re
from pathlib import Path

import pytest

from app.config import get_settings
from tests.conftest import AUTH, OTHER_AUTH
from tests.unit.test_errors import assert_error_format

STUDIO = Path(__file__).resolve().parents[2] / "app" / "studio"
URL = "/api/v1/broll/jobs"


# ---------- The page + static files ----------

def test_root_serves_the_studio_page_with_security_headers(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "<title>B-roll Studio</title>" in resp.text
    assert "default-src 'self'" in resp.headers["content-security-policy"]
    assert resp.headers["referrer-policy"] == "no-referrer"  # signed links never leak as a Referer
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_every_file_the_page_links_is_served_with_the_right_type(client):
    page = client.get("/").text
    linked = set(re.findall(r'(?:src|href)="/studio/([^"]+)"', page))
    assert {"studio.css", "app.js", "icon.svg"} <= linked
    # app.js imports the other modules: they must be served too.
    imports = set(re.findall(r'from "\./([a-z]+\.js)"', client.get("/studio/app.js").text))
    assert {"api.js", "create.js", "dom.js", "result.js"} <= imports
    types = {"css": "text/css", "js": "text/javascript", "svg": "image/svg+xml"}
    for name in linked | imports:
        resp = client.get(f"/studio/{name}")
        assert resp.status_code == 200, name
        assert resp.headers["content-type"].startswith(types[name.rsplit(".", 1)[1]]), name
        assert resp.content == (STUDIO / name).read_bytes()


def test_studio_uses_no_external_resources():
    """No CDN, no web fonts: everything comes from our own server."""
    for path in STUDIO.iterdir():
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"https?://(?!www\.w3\.org/2000/svg)", text), f"external URL in {path.name}"


@pytest.mark.parametrize("name", ["nope.js", "..%2Fmain.py", "app.py", "studio.css.bak", ".env"])
def test_unknown_or_unsafe_studio_files_are_json_404(client, name):
    resp = client.get(f"/studio/{name}")
    assert resp.status_code == 404
    assert_error_format(resp, "NOT_FOUND")


def test_studio_can_be_switched_off(client, monkeypatch):
    monkeypatch.setenv("STUDIO_ENABLED", "false")
    get_settings.cache_clear()
    assert client.get("/").status_code == 404
    assert client.get("/studio/app.js").status_code == 404
    assert client.get("/health").status_code == 200  # the API itself is unaffected


def test_studio_dir_setting_is_used(client, monkeypatch, tmp_path):
    (tmp_path / "index.html").write_text("<title>custom</title>", encoding="utf-8")
    monkeypatch.setenv("STUDIO_DIR", str(tmp_path))
    get_settings.cache_clear()
    assert client.get("/").text == "<title>custom</title>"


def test_studio_cannot_be_framed_unless_allowed(client, monkeypatch):
    assert "frame-ancestors 'none'" in client.get("/").headers["content-security-policy"]
    monkeypatch.setenv("STUDIO_FRAME_ANCESTORS", "https://huggingface.co")
    get_settings.cache_clear()
    assert "frame-ancestors https://huggingface.co" in client.get("/").headers["content-security-policy"]
    monkeypatch.setenv("STUDIO_FRAME_ANCESTORS", "x; script-src *")  # no injecting other CSP rules
    get_settings.cache_clear()
    assert "frame-ancestors 'none'" in client.get("/").headers["content-security-policy"]


# ---------- GET /api/v1/jobs ----------

def test_job_list_needs_a_token(client):
    assert_error_format(client.get("/api/v1/jobs"), "UNAUTHORIZED")


def test_job_list_shows_only_my_jobs_newest_first(client):
    mine = [client.post(URL, json={"script": f"Hello number {i}."}, headers=AUTH).json()["job_id"] for i in range(3)]
    theirs = client.post(URL, json={"script": "Someone else."}, headers=OTHER_AUTH).json()["job_id"]

    resp = client.get("/api/v1/jobs", headers=AUTH)
    assert resp.status_code == 200
    jobs = resp.json()["jobs"]
    assert [j["job_id"] for j in jobs] == mine[::-1]
    assert theirs not in {j["job_id"] for j in jobs}
    assert set(jobs[0]) == {"job_id", "status", "progress", "stage", "input_type", "parent_job_id", "error_code",
                            "created_at", "finished_at", "duration_seconds"}
    assert jobs[0]["status"] == "queued" and jobs[0]["input_type"] == "script"
    assert jobs[0]["duration_seconds"] is None  # not finished yet

    limited = client.get("/api/v1/jobs?limit=2", headers=AUTH).json()["jobs"]
    assert [j["job_id"] for j in limited] == mine[:0:-1]


@pytest.mark.parametrize("limit", ["0", "101", "abc"])
def test_job_list_limit_is_checked(client, limit):
    resp = client.get(f"/api/v1/jobs?limit={limit}", headers=AUTH)
    assert resp.status_code == 422
    assert_error_format(resp, "INVALID_INPUT")


def test_finished_job_has_a_duration_and_error_code(client):
    from app.errors import AppError, ErrorCode
    from app.jobs import claim_next_job, finish_job

    job_id = client.post(URL, json={"script": "Hello."}, headers=AUTH).json()["job_id"]
    assert claim_next_job("w1") == job_id
    finish_job(job_id, error=AppError(ErrorCode.RENDER_FAILED, "boom", 500))
    (job,) = client.get("/api/v1/jobs", headers=AUTH).json()["jobs"]
    assert job["status"] == "failed" and job["error_code"] == "RENDER_FAILED"
    assert job["duration_seconds"] is not None and job["duration_seconds"] >= 0


# ---------- GET /api/v1/config/public ----------

def test_public_config_needs_a_token(client):
    assert_error_format(client.get("/api/v1/config/public"), "UNAUTHORIZED")


def test_public_config_has_the_ui_settings_and_no_secrets(client, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_secret_value")
    monkeypatch.setenv("PIXABAY_API_KEY", "pixabay_secret_value")
    get_settings.cache_clear()
    resp = client.get("/api/v1/config/public", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    s = get_settings()
    assert body["voices"] == list(s.tts_voices) and body["default_voice"] in body["voices"]
    assert body["aspect_ratios"] == ["16:9", "9:16", "1:1"]
    assert body["max_audio_seconds"] == s.MAX_AUDIO_SECONDS and body["max_upload_mb"] == s.MAX_UPLOAD_MB
    assert body["allowed_audio_extensions"] == ["m4a", "mp3", "wav"]
    text = resp.text
    for secret in ("gsk_secret_value", "pixabay_secret_value", "test-token", s.FILE_LINK_SECRET, s.CALLBACK_SECRET,
                   s.S3_SECRET_KEY):
        assert secret not in text
