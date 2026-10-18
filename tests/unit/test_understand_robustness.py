"""Session 7B: Groq in batches with 429 retry-after, re-asking only missing scenes, rules PER SCENE;
5 queries with concrete metaphors for abstract lines; a sensitive STORY makes every scene calm."""
import json
import time

from app.config import get_settings
from app.pipeline.safety import FESTIVE_TERMS, plan_search
from app.pipeline.scenes import Scene
from app.pipeline.understand import RulesUnderstander, understand_scenes
from tests.fake_api import FakeAPI


def setenv(monkeypatch, **values):
    for key, value in values.items():
        monkeypatch.setenv(key, str(value))
    get_settings.cache_clear()


def use_groq(monkeypatch, url, **extra):
    setenv(monkeypatch, UNDERSTAND_PROVIDER="groq", GROQ_API_KEY="k", GROQ_BASE_URL=url, LLM_TIMEOUT_SECONDS=2,
           **extra)


def scenes_of(texts):
    return [Scene(i, i * 3.0, i * 3.0 + 3.0, t) for i, t in enumerate(texts)]


def answer_for(indexes, story_sensitive=False):
    return {"story_sensitive": story_sensitive, "scenes": [
        {"index": i, "queries": [f"q{i} meaning", f"q{i} metaphor", f"q{i} setting", f"q{i} action", f"q{i} mood"],
         "flags": {}, "reason": f"scene {i}"} for i in indexes]}


def asked_indexes(call) -> list[int]:
    user = json.loads(json.loads(call["body"])["messages"][1]["content"])
    return [sc["index"] for sc in user["scenes"]]


def reply(content):
    return 200, {"choices": [{"message": {"content": json.dumps(content)}}], "usage": {"total_tokens": 10}}


TEXTS = ["Small shops open early.", "Cloud computing helps them grow.", "Customers come back every week.",
         "Families walk in the park.", "The market closes at night."]


def test_long_scripts_are_sent_in_batches_and_every_scene_answered(monkeypatch):
    def answer(method, path, query, body):
        user = json.loads(json.loads(body)["messages"][1]["content"])
        return reply(answer_for([sc["index"] for sc in user["scenes"]]))
    server = FakeAPI(answer)
    try:
        use_groq(monkeypatch, server.url, LLM_BATCH_SIZE=2)
        meanings, info, warnings = understand_scenes(scenes_of(TEXTS), " ".join(TEXTS))
    finally:
        server.close()
    assert [asked_indexes(c) for c in server.calls] == [[0, 1], [2, 3], [4]]
    assert all(m.source == "groq" for m in meanings) and warnings == []
    assert meanings[3].queries[1] == "q3 metaphor" and meanings[3].query_roles == ["meaning", "metaphor", "setting",
                                                                                     "action", "mood"]
    assert info["calls"] == 3 and info["fallback_scenes"] == []


def test_http_429_waits_retry_after_and_asks_again(monkeypatch):
    calls = {"n": 0}

    def answer(method, path, query, body):
        calls["n"] += 1
        if calls["n"] == 1:
            return 429, {"error": "rate limited"}, {"retry-after": "0.2"}
        return reply(answer_for([0, 1]))
    server = FakeAPI(answer)
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    try:
        use_groq(monkeypatch, server.url)
        meanings, info, warnings = understand_scenes(scenes_of(TEXTS[:2]), "x")
    finally:
        server.close()
    assert len(server.calls) == 2 and slept == [0.2]           # the server's own wait, then once more
    assert all(m.source == "groq" for m in meanings) and warnings == []
    assert info["rate_limited_answers"] == 1


def test_too_much_rate_limit_waiting_falls_back_to_rules(monkeypatch):
    server = FakeAPI(lambda *a: (429, {"error": "slow down"}, {"retry-after": "10"}))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    try:
        use_groq(monkeypatch, server.url, LLM_MAX_WAIT_SECONDS=25)
        meanings, info, warnings = understand_scenes(scenes_of(TEXTS[:2]), "x")
    finally:
        server.close()
    assert len(server.calls) == 3                               # 10 s + 10 s waited; a third wait would pass 25 s
    assert all(m.source == "rules" for m in meanings)
    assert "HTTP 429" in warnings[0] and "rules" in warnings[0]


def test_a_missing_scene_is_asked_again_alone_then_gets_the_rules_only_for_itself(monkeypatch):
    def answer(method, path, query, body):
        asked = [sc["index"] for sc in json.loads(json.loads(body)["messages"][1]["content"])["scenes"]]
        return reply(answer_for([i for i in asked if i != 1]))  # the LLM always forgets scene 1
    server = FakeAPI(answer)
    try:
        use_groq(monkeypatch, server.url)
        meanings, info, warnings = understand_scenes(scenes_of(TEXTS[:3]), " ".join(TEXTS[:3]))
    finally:
        server.close()
    assert [asked_indexes(c) for c in server.calls] == [[0, 1, 2], [1]]   # re-asked ONLY for the missing one
    assert [m.source for m in meanings] == ["groq", "rules", "groq"]       # rules per scene, not per job
    assert "server" in " ".join(meanings[1].queries)                       # the rules still read "cloud computing"
    assert "scene(s) 1" in warnings[0] and info["fallback_scenes"] == [1]


def test_a_later_answer_that_completes_the_batch_needs_no_rules(monkeypatch):
    calls = {"n": 0}

    def answer(method, path, query, body):
        calls["n"] += 1
        return reply(answer_for([0] if calls["n"] == 1 else [1]))
    server = FakeAPI(answer)
    try:
        use_groq(monkeypatch, server.url)
        meanings, info, warnings = understand_scenes(scenes_of(TEXTS[:2]), "x")
    finally:
        server.close()
    assert [m.source for m in meanings] == ["groq", "groq"] and warnings == []


def test_abstract_lines_get_concrete_filmable_metaphors_from_the_rules():
    meaning = RulesUnderstander().understand_one(0, "Trust is earned slowly and lost quickly.",
                                                 "Trust is earned slowly and lost quickly.")
    assert meaning.flags["abstract"] is True
    assert meaning.queries[:3] == ["two people shaking hands", "hand passing house keys",
                                   "friends helping each other climb"]
    assert len(meaning.queries) == get_settings().QUERIES_PER_SCENE == 5
    assert "concrete metaphors for trust" in meaning.reason


# The run-1 sad story (testpack/inputs/hard04_sensitive_news.json). In run 1 scenes 2-4 were NOT calm.
SAD_STORY = ["Heavy floods this week forced thousands of families to leave their homes.",
             "Rescue teams are still reaching some villages, and officials have confirmed that several people "
             "lost their lives.",
             "Schools and community halls have been opened as shelters, where volunteers are serving hot food and "
             "clean water.",
             "Doctors are visiting the camps to check on children and older people.",
             "If you would like to help, local charities are collecting blankets and dry food.",
             "Our thoughts are with every family who has lost someone."]


def test_a_sensitive_story_filters_every_scene_but_keeps_the_real_subject():
    scenes = scenes_of(SAD_STORY)
    meanings, info, _ = understand_scenes(scenes, " ".join(SAD_STORY))  # mock adapter + the code detectors
    assert [m.flags["sensitive"] for m in meanings] == [True, True, False, False, False, True]
    assert info["story_sensitive"] is True and "3 of 6 scenes" in info["story_sensitive_reason"]
    assert all(m.story_sensitive for m in meanings)
    for m, sc in zip(meanings, scenes):
        plan = plan_search(m, sc.text)
        assert not any(q.startswith("calm ") for q in plan.queries), plan.queries  # real subject, no prefix
        assert "blood" in plan.exclude_terms and set(FESTIVE_TERMS) <= set(plan.exclude_terms)
    neutral = plan_search(meanings[3], scenes[3].text)  # "Doctors are visiting the camps..."
    assert "sensitive story" in " ".join(neutral.notes)


def test_one_sad_word_in_a_happy_story_does_not_make_the_story_sensitive():
    texts = ["Our bakery opened in spring.", "The old oven died last year, so we bought a new one.",
             "Now we bake bread every morning.", "Customers love the cinnamon rolls."]
    meanings, info, _ = understand_scenes(scenes_of(texts), " ".join(texts))
    assert info["story_sensitive"] is False and not any(m.story_sensitive for m in meanings)
    assert not plan_search(meanings[2], texts[2]).queries[0].startswith("calm")


def test_the_llm_can_mark_the_whole_story_sensitive(monkeypatch):
    server = FakeAPI(lambda *a: reply(answer_for([0, 1], story_sensitive=True)))
    try:
        use_groq(monkeypatch, server.url)
        meanings, info, _ = understand_scenes(scenes_of(["A quiet town.", "People gathered at the hall."]), "x")
    finally:
        server.close()
    assert info["story_sensitive"] and all(m.story_sensitive for m in meanings)
    assert "the LLM rated the whole script as sensitive" in info["story_sensitive_reason"]
