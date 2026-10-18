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


def test_statistic_line_gets_an_animated_number_card_when_overlay_is_off(tmp_path, monkeypatch):
    setenv(monkeypatch, STATISTIC_OVERLAY="false")
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


# ---------- Logo check (mock scorer: MockProvider.logo_probs sets a clip's logo probability) ----------

def all_mock_ids(plan):
    return [c.id for q in plan.queries for c in MockProvider().search(q, "landscape", 4.0)]


def test_logo_decision_thresholds():
    from app.pipeline.rank import logo_decision

    s = get_settings()
    assert (s.LOGO_REJECT_PROB, s.LOGO_PENALTY_PROB) == (0.6, 0.4)
    assert logo_decision(0.95)[0] == "rejected"                               # clearly a logo
    assert logo_decision(0.61)[0] == "rejected"                               # just above LOGO_REJECT_PROB
    assert logo_decision(0.60)[:2] == ("penalised", s.LOGO_PENALTY)           # not ABOVE 0.6: kept, penalised
    assert logo_decision(0.45)[:2] == ("penalised", s.LOGO_PENALTY)
    result, penalty, facts = logo_decision(0.05)                              # clean
    assert (result, penalty) == ("ok", 0.0)
    assert facts == {"logo_prob": 0.05, "result": "ok", "reject_prob": 0.6, "penalty_prob": 0.4, "penalty": 0.0}
    assert logo_decision(None) == ("ok", 0.0, None)                           # LOGO_CHECK off / not checked


def test_logo_views_are_the_whole_picture_plus_a_grid_of_crops():
    from PIL import Image

    from app.pipeline.rank import logo_views

    views = logo_views(Image.new("RGB", (640, 360)), 3)
    assert len(views) == 10 and views[0].size == (640, 360) and views[1].size == (213, 120)
    assert len(logo_views(Image.new("RGB", (640, 360)), 0)) == 1


def test_only_candidates_that_pass_the_score_threshold_get_the_logo_check(tmp_path, monkeypatch):
    """The logo check costs CLIP time (10 views per picture), so clips that can never be chosen skip it."""
    from app.pipeline.rank import MockScorer

    checked = []
    real = MockScorer.logo_probs
    monkeypatch.setattr(MockScorer, "logo_probs", lambda self, c, p: checked.extend(c) or real(self, c, p))
    setenv(monkeypatch, STOCK_MIN_SCORE="0.29")  # mock scores here are 0.28-0.30: some pass, some do not
    scenes, meanings, plans = make(["Small shops open early every morning."])
    select_clip(scenes[0], meanings[0], plans[0], context(tmp_path))
    scorer = MockScorer()
    assert checked and all(scorer.score([c], [None], plans[0].queries)[0] >= 0.29 for c in checked)
    assert len(checked) < len(all_mock_ids(plans[0]))  # the low scorers were not checked


def test_candidate_that_shows_a_logo_is_rejected_with_the_reason(tmp_path, monkeypatch):
    scenes, meanings, plans = make(["Small shops open early every morning."])
    best = select_clip(scenes[0], meanings[0], plans[0], context(tmp_path / "a"))
    monkeypatch.setattr(MockProvider, "logo_probs", {best.candidate.id: 0.93})  # clearly a logo
    sel = select_clip(scenes[0], meanings[0], plans[0], context(tmp_path / "b"))
    assert sel.candidate.provider == "mock" and sel.candidate.id != best.candidate.id
    assert f"logo check rejected 1, best: {best.candidate.uid} (logo prob 0.93, scene" in sel.reason
    assert sel.logo_check["result"] == "ok" and sel.to_dict()["logo_check"] == sel.logo_check
    assert "logo check ok (logo prob 0.05)" in sel.reason


def test_possible_logo_candidates_are_penalised_but_still_usable(tmp_path, monkeypatch):
    scenes, meanings, plans = make(["Small shops open early every morning."])
    # Every clip is in the penalty band (LOGO_PENALTY_PROB 0.4 < prob <= LOGO_REJECT_PROB 0.6).
    monkeypatch.setattr(MockProvider, "logo_probs", {i: 0.5 for i in all_mock_ids(plans[0])})
    sel = select_clip(scenes[0], meanings[0], plans[0], context(tmp_path))
    assert sel.candidate.provider == "mock"
    assert sel.logo_check["result"] == "penalised"
    assert sel.logo_check["thumbnail"]["penalty"] == get_settings().LOGO_PENALTY
    assert sel.final_score < sel.clip_score + 0.05  # the penalty was taken off the order score
    assert "logo check penalised (logo prob 0.50)" in sel.reason


def test_clean_candidate_beats_a_penalised_one_with_a_slightly_higher_score(tmp_path, monkeypatch):
    scenes, meanings, plans = make(["Small shops open early every morning."])
    best = select_clip(scenes[0], meanings[0], plans[0], context(tmp_path / "a"))
    # Maybe some brand text: in the penalty band -> penalised, not rejected, so a clean near-tie wins.
    monkeypatch.setattr(MockProvider, "logo_probs", {best.candidate.id: 0.5})
    sel = select_clip(scenes[0], meanings[0], plans[0], context(tmp_path / "b"))
    assert sel.candidate.id != best.candidate.id and sel.logo_check["result"] == "ok"


def test_when_every_candidate_shows_a_logo_the_scene_gets_a_generated_card(tmp_path, monkeypatch):
    scenes, meanings, plans = make(["Apple launched a new laptop for its fans."])
    monkeypatch.setattr(MockProvider, "logo_probs", {i: 0.9 for i in all_mock_ids(plans[0])})
    sel = select_clip(scenes[0], meanings[0], plans[0], context(tmp_path))
    assert sel.candidate.provider == "generated" and sel.logo_check is None
    assert "logo check rejected" in sel.reason


def test_logo_check_can_be_switched_off(tmp_path, monkeypatch):
    scenes, meanings, plans = make(["Small shops open early every morning."])
    monkeypatch.setattr(MockProvider, "logo_probs", {i: 0.9 for i in all_mock_ids(plans[0])})
    monkeypatch.setenv("LOGO_CHECK", "false")
    get_settings.cache_clear()
    sel = select_clip(scenes[0], meanings[0], plans[0], context(tmp_path))
    assert sel.candidate.provider == "mock" and sel.logo_check is None and "logo" not in sel.reason


def test_logo_in_a_frame_of_the_downloaded_clip_rejects_it_even_with_a_clean_thumbnail(tmp_path, monkeypatch):
    """Must-have 6: the thumbnail is clean, but frame 2 of the REAL clip shows a logo -> next candidate."""
    scenes, meanings, plans = make(["Small shops open early every morning."])
    best = select_clip(scenes[0], meanings[0], plans[0], context(tmp_path / "a"))
    assert best.logo_check["thumbnail"]["result"] == "ok"
    assert [f["result"] for f in best.logo_check["frames"]] == ["ok", "ok", "ok"]  # LOGO_FRAME_COUNT=3
    assert [f["time"] for f in best.logo_check["frames"]] == [1.0, 3.0, 5.0]  # spread over the 6 s clip
    assert all(f["logo_prob"] == 0.05 for f in best.logo_check["frames"])

    monkeypatch.setattr(MockProvider, "frame_logo_probs", {best.candidate.id: [0.05, 0.90, 0.05]})
    warnings = []
    sel = select_clip(scenes[0], meanings[0], plans[0], context(tmp_path / "b", warnings))
    assert sel.candidate.provider == "mock" and sel.candidate.id != best.candidate.id
    assert f"{best.candidate.uid} rejected by the frame logo check (at 3.0 s, logo prob 0.90)" in sel.reason
    assert any("shows a logo at 3.0 s of the downloaded clip (logo prob 0.90 > 0.60)" in w for w in warnings)
    assert sel.logo_check["frames"] and all(f["result"] == "ok" for f in sel.logo_check["frames"])


def test_a_frame_in_the_penalty_band_does_not_reject_the_clip(tmp_path, monkeypatch):
    scenes, meanings, plans = make(["Small shops open early every morning."])
    best = select_clip(scenes[0], meanings[0], plans[0], context(tmp_path / "a"))
    monkeypatch.setattr(MockProvider, "frame_logo_probs", {best.candidate.id: [0.05, 0.55, 0.05]})
    sel = select_clip(scenes[0], meanings[0], plans[0], context(tmp_path / "b"))
    assert sel.candidate.id == best.candidate.id
    assert [f["result"] for f in sel.logo_check["frames"]] == ["ok", "penalised", "ok"]


def test_when_every_downloaded_clip_shows_a_logo_the_scene_gets_a_generated_card(tmp_path, monkeypatch):
    scenes, meanings, plans = make(["Small shops open early every morning."])
    monkeypatch.setattr(MockProvider, "frame_logo_probs", {i: [0.9] for i in all_mock_ids(plans[0])})
    sel = select_clip(scenes[0], meanings[0], plans[0], context(tmp_path))
    assert sel.candidate.provider == "generated" and "rejected by the frame logo check" in sel.reason
