"""Fixes from my ratings (2026-10-18): per-scene translation, best-effort footage instead of plain cards,
one rewritten query for borderline scenes. (The "no calm prefix" rule is in test_safety / test_understand_robustness.)"""
import json
from types import SimpleNamespace

from app.pipeline import runner
from app.pipeline.providers import REGISTRY
from app.pipeline.rank import select_clip, select_clips
from app.pipeline.scenes import Scene
from app.pipeline.understand import rules_concrete_query, translate_scenes
from tests.fake_api import FakeAPI
from tests.unit.test_footage_quality import LINE, Photo, Scored, cid, context, make, setenv, two_sources  # noqa: F401


def groq_answer(content):
    return lambda *a: (200, {"choices": [{"message": {"content": json.dumps(content)}}], "usage": {"total_tokens": 50}})


def tamil_scenes():
    return [Scene(0, 0.0, 4.0, "காலையில் மொபைல்"), Scene(1, 4.0, 8.0, "சிறிய எல்லைகள்")]


def test_each_scene_is_translated_on_its_own(monkeypatch):
    server = FakeAPI(groq_answer({"scenes": [{"index": 0, "english": "Phone in the morning"},
                                             {"index": 1, "english": "Small limits"}]}))
    try:
        setenv(monkeypatch, UNDERSTAND_PROVIDER="groq", GROQ_API_KEY="k", GROQ_BASE_URL=server.url)
        english, facts = translate_scenes(tamil_scenes(), "ta")
    finally:
        server.close()
    assert english == {0: "Phone in the morning", 1: "Small limits"} and facts["calls"] == 1
    sent = json.loads(json.loads(server.calls[0]["body"])["messages"][1]["content"])
    assert [s["text"] for s in sent["scenes"]] == ["காலையில் மொபைல்", "சிறிய எல்லைகள்"]  # one entry per scene


def test_scenes_groq_misses_fall_back_to_the_whisper_translation(monkeypatch):
    server = FakeAPI(groq_answer({"scenes": [{"index": 0, "english": "Phone in the morning"}]}))
    whisper = SimpleNamespace(translate=lambda path, lang: [("first part", 0.0, 4.0), ("second part", 4.0, 8.0)])
    run = SimpleNamespace(warn=lambda m: None, models={})
    scenes = tamil_scenes()
    try:
        setenv(monkeypatch, UNDERSTAND_PROVIDER="groq", GROQ_API_KEY="k", GROQ_BASE_URL=server.url)
        runner._english_for_search(run, whisper, None, "ta", scenes)
    finally:
        server.close()
    assert [sc.search_text for sc in scenes] == ["Phone in the morning", "second part"]
    facts = run.models["transcribe"]["translate_for_search"]
    assert facts["scenes_by_groq"] == 1 and facts["scenes_by_whisper"] == 1


def test_usable_footage_beats_a_plain_card(tmp_path, monkeypatch, two_sources):
    monkeypatch.setattr(Scored, "SCORES", [0.20])  # below 0.22 and no judge: the adaptive pass cannot help
    monkeypatch.setattr(Photo, "SCORES", [0.18])
    setenv(monkeypatch, BORDERLINE_SCORE=0)
    scenes, meanings, plans = make([LINE])
    sel = select_clip(scenes[0], meanings[0], plans[0], context(tmp_path))
    assert not sel.candidate.generated and sel.candidate.id == cid("vid", 0)
    assert sel.details["best_effort"] is True and sel.overlay["kind"] == "keyword" and sel.overlay["headline"]
    assert sel.reason.startswith("best available footage")


def test_a_plain_card_only_when_no_footage_is_usable(tmp_path, monkeypatch, two_sources):
    monkeypatch.setattr(Scored, "SCORES", [0.10])
    monkeypatch.setattr(Photo, "SCORES", [0.12])
    setenv(monkeypatch, BORDERLINE_SCORE=0)
    scenes, meanings, plans = make([LINE])
    assert select_clip(scenes[0], meanings[0], plans[0], context(tmp_path)).candidate.generated


def test_a_statistic_line_on_best_effort_footage_gets_its_number_badge(tmp_path, monkeypatch, two_sources):
    monkeypatch.setattr(Scored, "SCORES", [0.20])
    monkeypatch.setattr(Photo, "SCORES", [0.10])
    setenv(monkeypatch, BORDERLINE_SCORE=0)
    scenes, meanings, plans = make(["Our sales grew 40% in 2025."])
    sel = select_clip(scenes[0], meanings[0], plans[0], context(tmp_path))
    assert not sel.candidate.generated and sel.overlay["headline"] == "40%"


class Rewritable(Scored):
    """Weak results for the normal queries; one strong candidate only for BOOST_QUERY."""
    BOOST_QUERY = None

    def search(self, query, orientation, min_duration):
        out = super().search(query, orientation, min_duration)
        if query == self.BOOST_QUERY:
            c = self._candidate("c000000001", query, 0)
            c.extra["score"] = 0.31
            out.append(c)
        return out


def test_a_borderline_scene_searches_one_rewritten_query_and_takes_the_better_result(tmp_path, monkeypatch, two_sources):
    monkeypatch.setitem(REGISTRY, "vid", Rewritable)
    monkeypatch.setattr(Scored, "SCORES", [0.23])  # passes 0.22 but is below BORDERLINE_SCORE 0.25
    setenv(monkeypatch, PROVIDER_ORDER="vid,generated")
    scenes, meanings, plans = make([LINE])
    rewritten = rules_concrete_query(LINE, plans[0].queries)  # mock understander -> the offline rewrite
    monkeypatch.setattr(Rewritable, "BOOST_QUERY", rewritten)
    selections, facts = select_clips(scenes, meanings, plans, context(tmp_path))
    assert rewritten and selections[0].candidate.id == "c000000001"
    assert f"rewritten query '{rewritten}' (rules)" in selections[0].reason
    assert facts["borderline"]["borderline_scenes"] == 1 and facts["borderline"]["rewritten"] == 1
