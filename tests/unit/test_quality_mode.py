"""QUALITY_MODE (fast | balanced | best), early exit, and the judge that never waits on a rate limit."""
import time

import pytest

from app.config import QUALITY_PRESETS, Settings, get_settings
from app.pipeline import vlm
from app.pipeline.outputs import MANIFEST_SETTINGS
from app.pipeline.providers import get_provider
from app.pipeline.providers.base import Candidate
from app.pipeline.rank import get_ranking, select_clip, select_clips
from tests.fake_api import FakeAPI
from tests.unit.test_footage_quality import LINE, Scored, context, make, picture, setenv, two_sources  # noqa: F401


@pytest.mark.parametrize("mode", ["fast", "balanced", "best"])
def test_each_mode_fills_in_its_preset(monkeypatch, mode):
    for name in QUALITY_PRESETS[mode]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("QUALITY_MODE", mode)
    s = Settings()
    assert {k: getattr(s, k) for k in QUALITY_PRESETS[mode]} == QUALITY_PRESETS[mode]


def test_balanced_is_the_default_and_matches_the_brief(monkeypatch):
    for name in ["QUALITY_MODE", "LLM_BATCH_SIZE", "VLM_MAX_RETRY_AFTER_SECONDS", *QUALITY_PRESETS["balanced"]]:
        monkeypatch.delenv(name, raising=False)
    s = Settings()
    assert s.QUALITY_MODE == "balanced"
    assert s.MAX_CANDIDATES_PER_SOURCE == 15 and s.VLM_TOP_K == 3 and s.EARLY_EXIT and not s.VLM_JUDGE_CONFIDENT
    assert s.LLM_BATCH_SIZE == 6 and s.VLM_MAX_RETRY_AFTER_SECONDS == 0
    assert "QUALITY_MODE" in MANIFEST_SETTINGS


def test_a_setting_you_set_yourself_beats_the_mode(monkeypatch):
    monkeypatch.setenv("QUALITY_MODE", "best")
    monkeypatch.setenv("MAX_CANDIDATES_PER_SOURCE", "7")
    assert Settings().MAX_CANDIDATES_PER_SOURCE == 7


def test_fast_mode_switches_the_judge_off(monkeypatch):
    setenv(monkeypatch, QUALITY_MODE="fast", VLM_PROVIDER="mock")
    assert vlm.get_judge() is None
    setenv(monkeypatch, QUALITY_MODE="balanced")
    assert vlm.get_judge() is not None


def test_early_exit_stops_at_the_first_clear_winner(tmp_path, monkeypatch, two_sources):
    setenv(monkeypatch, PROVIDER_ORDER="vid,generated", QUALITY_MODE="balanced", EARLY_EXIT_STEP=2,
           MULTI_FRAME_TOP_K=0)
    monkeypatch.setattr(Scored, "SCORES", [0.30, 0.10, 0.10, 0.26, 0.10, 0.10])
    scenes, meanings, plans = make([LINE])
    ranking = get_ranking(get_provider("vid"), scenes[0], plans[0], context(tmp_path))
    assert len(ranking.rows) == 2 and ranking.stopped_after == 2 and len(ranking.rest) == 4


def test_without_early_exit_everything_is_scored(tmp_path, monkeypatch, two_sources):
    setenv(monkeypatch, PROVIDER_ORDER="vid,generated", QUALITY_MODE="best", MULTI_FRAME_TOP_K=0)
    monkeypatch.setattr(Scored, "SCORES", [0.30, 0.10, 0.10, 0.26, 0.10, 0.10])
    scenes, meanings, plans = make([LINE])
    ranking = get_ranking(get_provider("vid"), scenes[0], plans[0], context(tmp_path))
    assert len(ranking.rows) == 6 and ranking.stopped_after is None and not ranking.rest


def test_pass_2_scores_the_skipped_candidates_when_the_winner_is_taken(tmp_path, monkeypatch, two_sources):
    setenv(monkeypatch, PROVIDER_ORDER="vid,generated", EARLY_EXIT_STEP=2, MULTI_FRAME_TOP_K=0)
    monkeypatch.setattr(Scored, "SCORES", [0.30, 0.10, 0.10, 0.26])
    scenes, meanings, plans = make([LINE])
    ctx = context(tmp_path)
    ranking = get_ranking(get_provider("vid"), scenes[0], plans[0], ctx)
    ctx.used.add(ranking.rows[0].candidate.uid)  # the clear winner is already in the video
    sel = select_clip(scenes[0], meanings[0], plans[0], ctx)
    assert not sel.candidate.generated and sel.clip_score == pytest.approx(0.26)
    assert "early exit had stopped after 2 candidates; scored the other 2" in sel.reason


def test_balanced_judges_only_uncertain_scenes(tmp_path, monkeypatch, two_sources):
    setenv(monkeypatch, PROVIDER_ORDER="vid,generated", VLM_PROVIDER="mock")
    monkeypatch.setattr(Scored, "SCORES", [0.30])  # confident: >= 0.22 + 0.03
    scenes, meanings, plans = make([LINE])
    ctx = context(tmp_path, judge=vlm.get_judge())
    _, facts = select_clips(scenes, meanings, plans, ctx)
    assert facts["quality_mode"] == "balanced" and facts["vlm"]["scenes_judged"] == 0
    assert "confident" in ctx.vlm[0]["skipped"]


def test_a_rate_limited_judge_is_skipped_at_once(tmp_path, monkeypatch):
    server = FakeAPI(lambda *a: (429, {"error": "slow down"}, {"retry-after": "5"}))
    items = [("p:a", Candidate("p", "a", "video"), picture(tmp_path / "a.jpg", "red"))]
    try:
        setenv(monkeypatch, VLM_PROVIDER="groq", GROQ_API_KEY="k", GROQ_BASE_URL=server.url)
        started = time.perf_counter()
        with pytest.raises(vlm.JudgeUnavailable):
            vlm.get_judge().judge("x", None, items)
    finally:
        server.close()
    assert len(server.calls) == 1 and time.perf_counter() - started < 3  # no 5 s wait, no second call
    get_settings.cache_clear()
