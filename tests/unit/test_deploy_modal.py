"""The Modal demo (deploy/modal/), and the tunnel fallback keep the rules the live demo
depends on: no secrets in the repo, one container, scale to zero, cost guards on.
Most checks read the files as text, so they also run where the modal package is not installed (Docker/CI)."""
import importlib.util
import re
import sqlite3
from pathlib import Path

import pytest
import yaml

from app.db import get_engine

ROOT = Path(__file__).resolve().parents[2]
MODAL_DIR = ROOT / "deploy" / "modal"
MODAL_APP = (MODAL_DIR / "modal_app.py").read_text(encoding="utf-8")
TUNNEL = (ROOT / "scripts" / "tunnel.ps1").read_text(encoding="utf-8")


def load_modal_app():
    pytest.importorskip("modal")
    spec = importlib.util.spec_from_file_location("modal_app_under_test", MODAL_DIR / "modal_app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # builds the definitions only; nothing is deployed or contacted
    return module


def test_no_secret_values_in_the_modal_folder():
    for path in MODAL_DIR.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"(ghp_|github_pat_|hf_[A-Za-z0-9]{20}|gsk_)[A-Za-z0-9_]{10,}", text), path.name
        assert not re.search(r"\b(API_TOKENS|FILE_LINK_SECRET|CALLBACK_SECRET|GROQ_API_KEY|PIXABAY_API_KEY)\s*[=:]\s*\S",
                             text), path.name
    assert 'modal.Secret.from_name("broll-secrets")' in MODAL_APP


def test_one_container_scale_to_zero_and_the_volume():
    for line in ("min_containers=0", "max_containers=1", "@modal.concurrent(", "@modal.asgi_app(",
                 'modal.Volume.from_name("broll-data"', '"STORAGE_BACKEND": "local"', '"SQLITE_JOURNAL_MODE": "DELETE"',
                 '"WORKER_CONCURRENCY": "1"', '"FOOTAGE_WORKERS": "2"', '"ALLOW_PRIVATE_URLS": "false"',
                 "recover_interrupted_jobs()", "after_job=", "vol.commit()"):
        assert line in MODAL_APP, line


def test_cost_guard_defaults():
    assert 'knob("DEMO_MAX_JOBS_PER_DAY", "20")' in MODAL_APP
    assert 'knob("DEMO_MAX_JOBS_PER_MONTH", "50")' in MODAL_APP
    assert 'knob("DEMO_MAX_AUDIO_SECONDS", "600")' in MODAL_APP
    assert 'knob("MODAL_SCALEDOWN_SECONDS", "180")' in MODAL_APP
    assert "scaledown_window=SCALEDOWN_SECONDS" in MODAL_APP


def test_image_rules():
    req = (ROOT / "requirements-ml.txt").read_text(encoding="utf-8")
    pins = re.search(r"TORCH_PINS = \[(.*)\]", MODAL_APP).group(1)
    for pin in re.findall(r'"(torch(?:vision)?==[^"]+)"', pins):
        assert pin in req, f"{pin} differs from requirements-ml.txt"
    assert 'debian_slim(python_version="3.11")' in MODAL_APP
    for package in ("ffmpeg", "espeak-ng", "fonts-dejavu-core"):
        assert f'"{package}"' in MODAL_APP
    ignore = re.search(r"IGNORE = \[(.*?)\]", MODAL_APP, re.S).group(1)
    for pattern in (".env", ".venv", "data", "testpack", "*.xlsx", ".git"):
        assert f'"{pattern}"' in ignore, pattern
    # Models: downloaded BEFORE the code (cached), then checked offline AFTER the code, offline at run time.
    assert MODAL_APP.index("download_models.py") < MODAL_APP.index("add_local_dir(REPO")
    assert "HF_HUB_OFFLINE=1 python" in MODAL_APP and '.env({"HF_HUB_OFFLINE": "1"})' in MODAL_APP


def test_modal_definitions_build_locally():
    m = load_modal_app()
    assert m.app.name == "broll-studio"
    assert m.CPU == 2.0 and m.MEMORY_MB <= m.MEMORY_LIMIT_MB
    assert m.DEMO_ENV["DEMO_MAX_JOBS_PER_DAY"] and m.DEMO_ENV["MAX_AUDIO_SECONDS"]
    assert m.DEMO_ENV["DEMO_MAX_JOBS_PER_MONTH"] and m.SCALEDOWN_SECONDS == 180


def test_volume_commit_holds_the_database_write_lock(tmp_path):
    """While the Volume is committed, no other connection can write the database (it would wait)."""
    m = load_modal_app()
    db_path = get_engine().url.database
    seen = {}

    class FakeVolume:
        def commit(self):
            other = sqlite3.connect(db_path, timeout=0)
            try:
                other.execute("BEGIN IMMEDIATE")
                seen["could_write"] = True
            except sqlite3.OperationalError as e:
                seen["could_write"] = False
                seen["error"] = str(e)
            finally:
                other.close()

    m.commit_with_db_locked(FakeVolume(), get_engine())
    assert seen == {"could_write": False, "error": "database is locked"}
    other = sqlite3.connect(db_path, timeout=0)  # and the lock is released afterwards
    other.execute("BEGIN IMMEDIATE")
    other.rollback()
    other.close()


def test_force_https_marks_requests_as_https():
    import asyncio

    m = load_modal_app()
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["scheme"] if scope["type"] == "http" else scope["type"])

    wrapped = m.force_https(inner)
    asyncio.run(wrapped({"type": "http", "scheme": "http"}, None, None))
    asyncio.run(wrapped({"type": "lifespan"}, None, None))
    assert seen == ["https", "lifespan"]



def test_tunnel_script_rules():
    assert "tools/bin/" in (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "Get-AuthenticodeSignature" in TUNNEL and "Cloudflare" in TUNNEL  # signed binary only
    assert '"dev-token"' in TUNNEL  # refuses the example token on a public URL
    assert "$env:PUBLIC_BASE_URL = $publicUrl" in TUNNEL  # https links
    assert '$env:ALLOW_PRIVATE_URLS = "false"' in TUNNEL
