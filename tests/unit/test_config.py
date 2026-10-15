"""Company rule: .env.example must list EVERY setting the app reads."""
import re
from pathlib import Path

from app.config import Settings, get_settings

ROOT = Path(__file__).resolve().parents[2]


def test_env_example_lists_every_setting():
    listed = set(re.findall(r"^([A-Z0-9_]+)=", (ROOT / ".env.example").read_text(), flags=re.M))
    assert set(Settings.model_fields) - listed == set(), "add these to .env.example"
    assert listed - set(Settings.model_fields) == set(), "remove these from .env.example (unknown)"


def test_settings_come_from_env(monkeypatch):
    monkeypatch.setenv("API_TOKENS", " a , b,, ")
    monkeypatch.setenv("WORKER_CONCURRENCY", "7")
    get_settings.cache_clear()
    s = get_settings()
    assert s.api_tokens == ["a", "b"]
    assert s.WORKER_CONCURRENCY == 7
