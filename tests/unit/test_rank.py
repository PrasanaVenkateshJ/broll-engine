"""The selection rule: no repeats, fallbacks, mid-job switch-off, corrupted downloads, threshold."""
import json

import pytest

from app.config import get_settings
from app.pipeline.providers import REGISTRY
from app.pipeline.providers.mock import MockProvider
from app.pipeline.rank import SelectionContext, get_scorer, select_clip, verify_media
from app.pipeline.safety import plan_search
from app.pipeline.scenes import Scene
from app.pipeline.understand import MockUnderstander


class BackupProvider(MockProvider):
    """A second mock source, so we can see the rule move on to 'the next provider'."""
    name = "backup"
    fail = False
    corrupt_ids: set[str] = set()


@pytest.fixture
def backup(monkeypatch):
    monkeypatch.setitem(REGISTRY, "backup", BackupProvider)


def setenv(monkeypatch, **values):
    for key, value in values.items():
        monkeypatch.setenv(key, str(value))
    get_settings.cache_clear()


def make(texts, avoid=None):
    scenes = [Scene(i, i * 4.0, i * 4.0 + 4.0, t) for i, t in enumerate(texts)]
    meanings = MockUnderstander().understand(scenes, " ".join(texts))
    plans = [plan_search(m, sc.text, avoid) for m, sc in zip(meanings, scenes)]
    return scenes, meanings, plans


def context(tmp_path, warnings=None):
    warnings = warnings if warnings is not None else []
    return SelectionContext(work=tmp_path, orientation="landscape", warn=warnings.append, scorer=get_scorer())


def test_picks_a_stock_clip_with_score_and_reason(tmp_path):
    scenes, meanings, plans = make(["Small shops open early every morning."])
    sel = select_clip(scenes[0], meanings[0], plans[0], context(tmp_path))
    assert sel.candidate.provider == "mock" and sel.path.is_file()
    assert sel.clip_score >= get_settings().STOCK_MIN_SCORE
    assert "mock: score" in sel.reason and ">= 0.22" in sel.reason and sel.candidate.query in sel.reason
    assert verify_media(sel.path, "video") is None


def test_no_clip_id_is_used_twice_in_one_video(tmp_path):
    texts = ["Small shops open early every morning."] * 6  # same text -> same search results every time
    scenes, meanings, plans = make(texts)
    ctx = context(tmp_path)
    uids = [select_clip(sc, m, p, ctx).candidate.uid for sc, m, p in zip(scenes, meanings, plans)]
    assert len(uids) == len(set(uids)) == 6


def test_provider_down_falls_back_to_the_next_provider_with_a_warning(tmp_path, monkeypatch, backup):
    setenv(monkeypatch, PROVIDER_ORDER="mock,backup,generated")
    monkeypatch.setattr(MockProvider, "fail", True)
    scenes, meanings, plans = make(["Small shops open early every morning."])
    warnings = []
    sel = select_clip(scenes[0], meanings[0], plans[0], context(tmp_path, warnings))
    assert sel.candidate.provider == "backup"
    assert "mock failed (mock provider is down)" in sel.reason
    assert warnings == ["Footage source mock failed (mock provider is down); used the next source"]


def test_all_stock_providers_down_gives_a_generated_card(tmp_path, monkeypatch):
    monkeypatch.setattr(MockProvider, "fail", True)
    scenes, meanings, plans = make(["Trust is earned slowly."])
    sel = select_clip(scenes[0], meanings[0], plans[0], context(tmp_path))
    assert sel.candidate.provider == "generated" and sel.candidate.generated and not sel.candidate.ai_generated
    assert sel.path is None and "generated motion card" in sel.reason


def test_provider_switched_off_mid_job_with_the_override_file(tmp_path, monkeypatch, backup):
    override = tmp_path / "override.json"
    setenv(monkeypatch, PROVIDER_ORDER="mock,backup,generated", PROVIDERS_OVERRIDE_FILE=str(override))
    scenes, meanings, plans = make(["Small shops open early.", "Busy streets at night."])
    ctx = context(tmp_path)
    first = select_clip(scenes[0], meanings[0], plans[0], ctx)
    override.write_text(json.dumps({"disabled": ["mock"]}), encoding="utf-8")  # switched off DURING the job
    second = select_clip(scenes[1], meanings[1], plans[1], ctx)
    assert first.candidate.provider == "mock"
    assert second.candidate.provider == "backup"
    assert "mock skipped (switched off by" in second.reason


def test_providers_disabled_env(tmp_path, monkeypatch):
    setenv(monkeypatch, PROVIDERS_DISABLED="mock")
    scenes, meanings, plans = make(["Small shops open early."])
    sel = select_clip(scenes[0], meanings[0], plans[0], context(tmp_path))
    assert sel.candidate.provider == "generated" and "PROVIDERS_DISABLED" in sel.reason


def test_corrupted_download_is_skipped_for_the_next_candidate(tmp_path, monkeypatch):
    scenes, meanings, plans = make(["Small shops open early every morning."])
    best = select_clip(scenes[0], meanings[0], plans[0], context(tmp_path / "a"))
    monkeypatch.setattr(MockProvider, "corrupt_ids", {best.candidate.id})
    warnings = []
    sel = select_clip(scenes[0], meanings[0], plans[0], context(tmp_path / "b", warnings))
    assert sel.candidate.provider == "mock" and sel.candidate.id != best.candidate.id
    assert f"{best.candidate.uid} corrupted (ffprobe cannot read it)" in sel.reason
    assert "is corrupted" in warnings[0]


def test_below_threshold_moves_on_and_ends_with_generated(tmp_path, monkeypatch):
    setenv(monkeypatch, STOCK_MIN_SCORE="0.5")  # mock scores are at most 0.30
    scenes, meanings, plans = make(["Small shops open early."])
    sel = select_clip(scenes[0], meanings[0], plans[0], context(tmp_path))
    assert sel.candidate.provider == "generated"
    assert "mock: best score 0.300 < 0.50" in sel.reason


def test_statistic_line_gets_an_animated_number_card(tmp_path):
    scenes, meanings, plans = make(["In 2025, household savings grew by 40 percent."])
    sel = select_clip(scenes[0], meanings[0], plans[0], context(tmp_path))
    assert sel.candidate.provider == "generated"
    assert sel.candidate.extra["headline"] == "40%"
    assert "statistic line" in sel.reason


def test_exclude_terms_drop_matching_candidates(tmp_path):
    # Every mock clip is tagged "mock"; avoiding it leaves nothing usable -> generated.
    scenes, meanings, plans = make(["Small shops open early."], avoid=["mock"])
    sel = select_clip(scenes[0], meanings[0], plans[0], context(tmp_path))
    assert sel.candidate.provider == "generated"
    assert "all excluded or already used" in sel.reason


def test_verify_media_rejects_garbage(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video" * 100)
    assert verify_media(bad, "video") == "ffprobe cannot read it"
