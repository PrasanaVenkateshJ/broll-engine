"""Tamil / Hindi narration: footage is searched in English, captions and cards stay in the spoken language."""
import json

from app.pipeline.scenes import Scene, attach_search_text, split_into_scenes
from app.pipeline.transcribe import Word
from app.pipeline.understand import RulesUnderstander, scene_card, understand_scenes
from tests.fake_api import FakeAPI
from tests.unit.test_understand import groq_answer, use_groq

TAMIL_SAVE = "பணத்தை சேமிப்பது முக்கியம்."          # "saving money is important."
TAMIL_SAD = "வெள்ளத்தில் பலர் உயிரிழந்தனர்."         # "many people died in the flood."


def tamil_scenes():
    return [Scene(0, 0.0, 3.0, TAMIL_SAVE, search_text="Saving money is important."),
            Scene(1, 3.0, 6.0, TAMIL_SAD, search_text="Many people died in the flood.")]


def test_translation_pieces_go_to_the_scene_that_holds_their_moment():
    scenes = [Scene(0, 0.0, 4.0, "a"), Scene(1, 4.0, 8.0, "b"), Scene(2, 8.0, 10.0, "c")]
    # One piece runs across the first cut: its words are spread over 2-6 s.
    attach_search_text(scenes, [("save money every week", 2.0, 6.0), ("big difference", 8.5, 9.5)])
    assert scenes[0].search_text == "save money"
    assert scenes[1].search_text == "every week"
    assert scenes[2].search_text == "big difference"
    attach_search_text(scenes, [])  # no translation: scenes search with their own text
    assert all(sc.search_text is None for sc in scenes)


def test_hindi_full_stop_danda_is_a_sentence_end():
    words = [Word("बचत", 0.0, 0.5), Word("ज़रूरी", 0.6, 1.2), Word("है।", 1.3, 2.4),
             Word("हर", 2.5, 3.0), Word("महीने", 3.1, 3.8), Word("बचाइए।", 3.9, 5.0)]
    scenes = split_into_scenes(words, 5.0, min_seconds=2.0, max_seconds=8.0)
    assert [sc.text for sc in scenes] == ["बचत ज़रूरी है।", "हर महीने बचाइए।"]


def test_rules_make_english_queries_from_the_translation_but_cards_from_the_spoken_text():
    meanings = RulesUnderstander().understand(tamil_scenes(), f"{TAMIL_SAVE} {TAMIL_SAD}")
    for meaning in meanings:
        assert all(q.isascii() for q in meaning.queries), meaning.queries
        assert "English translation" in meaning.reason
    assert any("money" in q or "coin" in q or "saving" in q for q in meanings[0].queries), meanings[0].queries
    # Safety detectors read the English too: the flood deaths are a sensitive topic.
    assert meanings[1].flags["sensitive"] is True
    # A card shows only the narration's own words (never the machine translation).
    assert meanings[0].card == scene_card(TAMIL_SAVE, None, [])


def test_without_translation_the_rules_find_no_english_words():
    """Why the translation is needed: the offline rules cannot read Tamil."""
    plain = [Scene(0, 0.0, 3.0, TAMIL_SAVE)]
    meaning = RulesUnderstander().understand(plain, TAMIL_SAVE)[0]
    assert meaning.queries[0] == "abstract background light"


def test_groq_gets_the_original_line_and_the_english_and_answers_english_queries(monkeypatch):
    content = {"scenes": [
        {"index": 0, "queries": ["piggy bank coins", "saving money jar", "family budget"], "flags": {},
         "reason": "saving money"},
        {"index": 1, "queries": ["calm flooded street", "rain clouds", "candles"], "flags": {"sensitive": True},
         "reason": "flood, calm imagery"}]}
    server = FakeAPI(groq_answer(content))
    try:
        use_groq(monkeypatch, server.url)
        meanings, _, _ = understand_scenes(tamil_scenes(), f"{TAMIL_SAVE} {TAMIL_SAD}")
    finally:
        server.close()
    sent = json.loads(json.loads(server.calls[0]["body"])["messages"][1]["content"])
    assert sent["scenes"][0] == {"index": 0, "text": TAMIL_SAVE, "visual_hint": None,
                                 "english": "Saving money is important."}
    assert meanings[0].queries[0] == "piggy bank coins"
    system = json.loads(server.calls[0]["body"])["messages"][0]["content"]
    assert "ALWAYS in English" in system


def test_english_scenes_send_the_same_request_as_before(monkeypatch):
    """No "english" key for English narration, so English jobs are unchanged."""
    server = FakeAPI(groq_answer({"scenes": [{"index": 0, "queries": ["a", "b", "c"], "flags": {}, "reason": "r"}]}))
    try:
        use_groq(monkeypatch, server.url)
        understand_scenes([Scene(0, 0.0, 3.0, "Save money.")], "Save money.")
    finally:
        server.close()
    sent = json.loads(json.loads(server.calls[0]["body"])["messages"][1]["content"])
    assert "english" not in sent["scenes"][0]
