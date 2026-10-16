"""Scene understanding: double meanings, statistics, names, sensitive lines, Groq + fallback."""
import json

import pytest

from app.config import get_settings
from app.pipeline.scenes import Scene
from app.pipeline.understand import (RulesUnderstander, detect_names, detect_sensitive, detect_statistic,
                                     understand_scenes)
from tests.fake_api import FakeAPI


def rules(text, script=None, hint=None):
    return RulesUnderstander().understand_one(0, text, script or text, hint)


@pytest.mark.parametrize("text, expected, not_expected", [
    ("Cloud computing now lets small businesses work from anywhere.", "server", "sky"),
    ("Look at the white clouds drifting across the blue sky.", "sky", "server"),
    ("She opened a savings account at the bank to get a loan.", "bank building", "river"),
    ("We sat on the bank of the river and watched the water.", "river", "atm"),
    ("Click the mouse to open the file on your computer.", "computer mouse", "grass"),
    ("A tiny mouse ran across the field looking for cheese.", "mouse in grass", "computer"),
    ("Learn Python to write your first program and code faster.", "code", "snake"),
    ("The python wrapped itself around a tree in the jungle.", "snake", "code"),
    ("Apple launched a new iPhone for its tech fans.", "smartphone", "orchard"),
    ("An apple a day is a healthy fresh fruit habit.", "apples", "smartphone"),
    ("The value of your property and stock can rise.", "coins", "family"),
    ("Honesty, respect and family are the values we live by.", "hands holding", "price"),
])
def test_double_meaning_words_follow_the_context(text, expected, not_expected):
    meaning = rules(text)
    joined = " ".join(meaning.queries).lower()
    assert expected in joined, meaning.queries
    assert not_expected not in joined, meaning.queries
    assert len(meaning.queries) == 3


def test_the_whole_script_gives_context_to_a_short_line():
    script = "Our software team stores all customer data online. Move everything to the cloud."
    meaning = rules("Move everything to the cloud.", script)
    assert "server" in " ".join(meaning.queries)


def test_apple_company_is_flagged_as_brand_and_fruit_is_not():
    assert rules("Apple launched a new iPhone for its tech fans.").flags["brand"] is True
    assert rules("An apple a day is a healthy fresh fruit habit.").flags["brand"] is False


@pytest.mark.parametrize("text, number", [
    ("Sales grew 40% in 2025.", "40%"),
    ("In 2025, household savings grew by 40 percent.", "40%"),
    ("The company raised $5 million last year.", "$5 million"),
    ("Prices doubled in a decade.", "doubled"),
    ("We now serve 300 customers.", "300"),
])
def test_numbers_are_statistics(text, number):
    meaning = rules(text)
    assert meaning.flags["statistic"] is True
    assert meaning.number == number == detect_statistic(text)


def test_a_year_alone_is_not_a_statistic():
    assert detect_statistic("In 2025 we moved to a new office.") is None
    assert rules("In 2025 we moved to a new office.").flags["statistic"] is False


def test_named_person_and_brand_detection():
    people, brands = detect_names("Elon Musk met Dr Jane Smith at Google in New York.")
    assert people == ["Elon Musk", "Jane Smith"]
    assert brands == ["Google"]  # New York is a place, not a person
    meaning = rules("Elon Musk founded a rocket company.")
    assert meaning.flags["named_person"] is True
    assert "Elon Musk" in meaning.names
    assert all("musk" not in q.lower() for q in meaning.queries)  # no name in the search queries
    assert "musk" not in meaning.keyword


def test_capitalised_first_word_is_not_a_name():
    assert detect_names("Saving money is not about earning more.") == ([], [])
    assert detect_names("Trust is earned slowly, one honest decision at a time.") == ([], [])


def test_sensitive_lines_are_flagged_with_calm_queries():
    meaning = rules("Thousands of people died in the earthquake.")
    assert meaning.flags["sensitive"] is True
    assert detect_sensitive("Thousands of people died in the earthquake.") == ["died", "earthquake"]
    assert not any(word in " ".join(meaning.queries) for word in ("blood", "body", "rubble"))


def test_abstract_line_is_flagged():
    meaning = rules("Trust is earned slowly, one honest decision at a time.")
    assert meaning.flags["abstract"] is True
    assert meaning.keyword == "trust"


def test_visual_hint_is_the_first_query():
    meaning = rules("Money grows when you save.", hint="coins in a glass jar")
    assert meaning.queries[0] == "coins in a glass jar"
    assert "visual_hint" in meaning.reason


# ---------- Groq adapter (a fake Groq server on 127.0.0.1) ----------

SCENES = [Scene(0, 0.0, 3.0, "Cloud computing lets small firms grow."),
          Scene(1, 3.0, 6.0, "Elon Musk said sales grew 40%.")]


def groq_answer(content: dict, status: int = 200):
    def answer(method, path, query, body):
        if status != 200:
            return status, {"error": "boom"}
        return 200, {"choices": [{"message": {"content": json.dumps(content)}}], "usage": {"total_tokens": 321}}
    return answer


def use_groq(monkeypatch, url, key="test-groq-key"):
    monkeypatch.setenv("UNDERSTAND_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", key)
    monkeypatch.setenv("GROQ_BASE_URL", url)
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "2")
    get_settings.cache_clear()


def test_groq_answer_is_used_and_safety_flags_are_never_weaker(monkeypatch):
    content = {"scenes": [
        {"index": 0, "queries": ["data center server racks", "network cables", "laptop in office"],
         "flags": {"abstract": False}, "reason": "cloud means computing here"},
        {"index": 1, "queries": ["rising sales chart", "busy shop counter", "Elon Musk portrait"],
         "flags": {"statistic": True}, "reason": "sales growth"}]}
    server = FakeAPI(groq_answer(content))
    try:
        use_groq(monkeypatch, server.url)
        meanings, info, warnings = understand_scenes(SCENES, " ".join(s.text for s in SCENES))
    finally:
        server.close()
    assert warnings == []
    assert meanings[0].queries[0] == "data center server racks"
    assert meanings[0].reason.startswith("groq:")
    # The LLM forgot named_person; the code detector adds it back.
    assert meanings[1].flags["named_person"] is True and meanings[1].flags["statistic"] is True
    assert info["model"] == "openai/gpt-oss-120b" and info["tokens_used"] == 321
    sent = json.loads(server.calls[0]["body"])
    assert sent["response_format"] == {"type": "json_object"}
    assert server.calls[0]["headers"]["Authorization"] == "Bearer test-groq-key"


def test_groq_failure_retries_once_then_falls_back_to_rules_with_warning(monkeypatch):
    server = FakeAPI(groq_answer({}, status=500))
    try:
        use_groq(monkeypatch, server.url)
        meanings, info, warnings = understand_scenes(SCENES, "x")
    finally:
        server.close()
    assert len(server.calls) == 2  # first try + LLM_RETRIES=1
    assert info["fallback"] == "rules"
    assert meanings[0].source == "rules" and "server" in " ".join(meanings[0].queries)
    assert len(warnings) == 1 and "Groq failed" in warnings[0] and "rules" in warnings[0]


def test_groq_with_bad_json_falls_back(monkeypatch):
    server = FakeAPI(lambda *a: (200, {"choices": [{"message": {"content": "not json at all"}}]}))
    try:
        use_groq(monkeypatch, server.url)
        meanings, _, warnings = understand_scenes(SCENES, "x")
    finally:
        server.close()
    assert meanings[0].source == "rules"
    assert "not valid JSON" in warnings[0]


def test_groq_without_key_falls_back_without_calling_the_network(monkeypatch):
    use_groq(monkeypatch, "http://127.0.0.1:9", key="")
    meanings, _, warnings = understand_scenes(SCENES, "x")
    assert meanings[0].source == "rules"
    assert "GROQ_API_KEY is empty" in warnings[0]


# ---------- Repeatable Groq answers: temperature 0 + stored in timeline.json + reused ----------

GOOD_ANSWER = {"scenes": [
    {"index": 0, "queries": ["data center server racks", "network cables", "laptop in office"],
     "flags": {}, "reason": "cloud means computing here"},
    {"index": 1, "queries": ["rising sales chart", "busy shop counter", "coins stacking"],
     "flags": {"statistic": True}, "reason": "sales growth"}]}
SCRIPT = " ".join(s.text for s in SCENES)


def as_timeline(meanings):
    """What build_timeline() stores per scene (only the part reuse needs)."""
    return {"scenes": [{"index": m.index, "understanding": m.to_dict()} for m in meanings]}


def test_groq_request_is_deterministic(monkeypatch):
    server = FakeAPI(groq_answer(GOOD_ANSWER))
    try:
        use_groq(monkeypatch, server.url)
        understand_scenes(SCENES, SCRIPT)
        understand_scenes(SCENES, SCRIPT)
    finally:
        server.close()
    first, second = (json.loads(c["body"]) for c in server.calls)
    assert first == second                                   # same script -> exactly the same request
    assert first["temperature"] == 0.0 and first["seed"] == get_settings().SEED


def test_stored_understanding_is_reused_without_asking_groq_again(monkeypatch):
    server = FakeAPI(groq_answer(GOOD_ANSWER))
    try:
        use_groq(monkeypatch, server.url)
        first, info, _ = understand_scenes(SCENES, SCRIPT)
        again, again_info, warnings = understand_scenes(SCENES, SCRIPT, previous_timeline=as_timeline(first))
    finally:
        server.close()
    assert len(server.calls) == 1                            # the re-render did NOT call the LLM
    assert [m.to_dict() for m in again] == [m.to_dict() for m in first]
    assert again_info["reused_from_timeline"] is True and warnings == []
    assert info["prompt_version"] and len(first[0].fingerprint) == 16


def test_stored_understanding_is_not_reused_when_the_script_changed(monkeypatch):
    server = FakeAPI(groq_answer(GOOD_ANSWER))
    try:
        use_groq(monkeypatch, server.url)
        first, _, _ = understand_scenes(SCENES, SCRIPT)
        changed = [Scene(0, 0.0, 3.0, "Cloud storage keeps family photos safe."), SCENES[1]]
        understand_scenes(changed, " ".join(s.text for s in changed), previous_timeline=as_timeline(first))
    finally:
        server.close()
    assert len(server.calls) == 2                            # different question -> asked again


def test_a_rules_fallback_is_never_reused_as_a_groq_answer(monkeypatch):
    server = FakeAPI(groq_answer({}, status=500))
    try:
        use_groq(monkeypatch, server.url)
        fallback, _, _ = understand_scenes(SCENES, SCRIPT)
        understand_scenes(SCENES, SCRIPT, previous_timeline=as_timeline(fallback))
    finally:
        server.close()
    assert fallback[0].source == "rules"
    assert len(server.calls) == 4                            # Groq is tried again (2 tries each time)
