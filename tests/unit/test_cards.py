"""Generated card text: built ONLY from the scene's own narration, every shown number verified.

The bug this guards against: the narration said "40%" but the card showed "21%" (a frame of the
old count-up animation). A card must never show a number or claim the narration does not say.
"""
import json
import re

import pytest

from app.config import get_settings
from app.pipeline.cards import build_card, check_card, find_figures, unverified_numbers
from app.pipeline.outputs import card_facts
from app.pipeline.providers.generated import GeneratedProvider
from app.pipeline.rank import Selection, SelectionContext, _card, get_scorer, select_clip
from app.pipeline.render import write_card_ass
from app.pipeline.safety import plan_search
from app.pipeline.scenes import Scene
from app.pipeline.understand import MockUnderstander, SceneMeaning, understand_scenes
from tests.fake_api import FakeAPI


@pytest.mark.parametrize("text, headline", [
    ("In 2025, household savings grew by 40%.", "40%"),
    ("sales grew 40 percent in 2025", "40%"),
    ("Nearly forty percent of families save nothing.", "40%"),
    ("Twenty-five per cent said yes.", "25%"),
    ("The startup's revenue hit $1.2 million last year.", "$1.2M"),
    ("A ticket costs Rs 500 in Chennai.", "Rs 500"),
    ("Output grew 3x after the change.", "3x"),
    ("It takes three times longer.", "3x"),
    ("We now serve 300 customers.", "300"),
])
def test_card_headline_is_the_main_figure_of_the_narration(text, headline):
    card = build_card(text)
    assert card.headline == headline and card.number == headline
    assert card.source == "narration"
    assert unverified_numbers(f"{card.headline} {card.subtitle}", text) == []


def test_percentage_or_money_beats_a_year():
    card = build_card("In 2025, household savings grew by 40%.")
    assert card.headline == "40%" and card.kind == "percent"
    assert card.subtitle == "household savings grew"  # a phrase from the same sentence
    assert build_card("In 2024 the shop earned $3 million.").headline == "$3M"


def test_a_year_only_line_shows_the_year():
    card = build_card("In 1969, humans walked on the moon.")
    assert card.headline == "1969" and card.kind == "year"
    assert card.subtitle == "humans walked on the moon"


def test_a_line_without_numbers_gets_a_keyword_card_with_no_number():
    card = build_card("Trust is earned slowly. One honest decision at a time.", keyword="trust")
    assert card.headline == "TRUST" and card.number is None and card.kind == "keyword"
    assert card.numbers_shown == [] and not any(ch.isdigit() for ch in card.headline + card.subtitle)


def test_number_words_without_a_unit_are_not_numbers():
    # "One honest decision" is not a statistic; "hours 500" is not "Rs 500".
    assert find_figures("One honest decision at a time.") == []
    # "500 hours" is a measure (shown with its unit); "a year" is not a number at all.
    assert [(f.kind, f.display) for f in find_figures("We work 500 hours a year.")] == [("measure", "500 hours")]


@pytest.mark.parametrize("text, headline", [
    ("The trail is twelve kilometres long.", "12 km"),
    ("It took forty minutes to get there.", "40 minutes"),
    ("Today, three out of four buyers read reviews.", "3 out of 4"),
    ("Last year, one in five people received a fake bank message.", "1 in 5"),
    ("1 in 5 people never check the sender.", "1 in 5"),
    ("Nearly forty percent of families save nothing.", "40%"),
    ("हमारी बचत दस प्रतिशत बढ़ी।", "10%"),                   # Hindi: "our savings grew ten percent"
    ("किसानों ने 10 प्रतिशत ज़्यादा पानी बचाया।", "10%"),        # digits + the Hindi word for percent
    ("पिछले साल पाँच लाख रुपये बचाए।", "Rs 5 lakh"),
    ("नौ हज़ार लोग आए।", "9K"),
    ("குழந்தைகள் பத்து சதவீதம் அதிக நேரம் செலவிடுகிறார்கள்.", "10%"),  # Tamil: "ten percent"
    ("இருபத்தைந்து சதவீதம் பேர் ஒப்புக்கொண்டனர்.", "25%"),       # 20 + 5 joined (sandhi)
    ("இருபத்து மூன்று கிலோமீட்டர் நடந்தோம்.", "23 km"),           # 20 + 3 as two words
])
def test_number_words_in_english_hindi_and_tamil_become_cards(text, headline):
    card = build_card(text)
    assert card.headline == headline
    assert card.source == "narration" and unverified_numbers(f"{card.headline} {card.subtitle}", text) == []


@pytest.mark.parametrize("text", ["One day, my grandmother smiled.", "Two years ago I opened a bakery.",
                                  "दो बच्चे खेल रहे थे।", "ஒரு நாள் நான் சென்றேன்."])
def test_small_number_words_in_ordinary_sentences_are_not_statistics(text):
    assert build_card(text).number is None


def test_a_card_number_not_said_in_hindi_or_tamil_is_still_rejected():
    assert unverified_numbers("20%", "हमारी बचत दस प्रतिशत बढ़ी।") == ["20%"]
    assert unverified_numbers("10%", "குழந்தைகள் பத்து சதவீதம்") == []
    assert unverified_numbers("11%", "குழந்தைகள் பத்து சதவீதம்") == ["11%"]


def test_the_subtitle_never_names_a_person_or_brand():
    card = build_card("Elon Musk said 40% of cars will be electric.", names=["Elon Musk"])
    assert card.headline == "40%" and "musk" not in card.subtitle.lower()
    assert card.subtitle == "cars will be electric"


@pytest.mark.parametrize("shown, narration, bad", [
    ("21%", "In 2025, household savings grew by 40%.", ["21%"]),
    ("40%", "In 2025, household savings grew by 40%.", []),
    ("40%", "sales grew forty percent", []),
    ("$1.2M", "revenue hit $1.2 million", []),
    ("2025", "In 2025, household savings grew by 40%.", []),
    ("40%", "we hired 40 people", ["40%"]),  # same digits, but 40 people is not 40 percent
])
def test_unverified_numbers_finds_every_number_the_narration_does_not_say(shown, narration, bad):
    assert unverified_numbers(shown, narration) == bad


# ---------- An LLM answer can never put a number on the card ----------

SCENE = Scene(0, 0.0, 4.0, "In 2025, household savings grew by 40%.")


def groq_server(card_suggestion: dict) -> FakeAPI:
    content = {"scenes": [{"index": 0, "queries": ["piggy bank coins", "family budget", "savings jar"],
                           "flags": {"statistic": True}, "reason": "savings growth", "card": card_suggestion}]}

    def answer(method, path, query, body):
        return 200, {"choices": [{"message": {"content": json.dumps(content)}}], "usage": {"total_tokens": 10}}
    return FakeAPI(answer)


def run_groq(monkeypatch, card_suggestion):
    server = groq_server(card_suggestion)
    try:
        monkeypatch.setenv("UNDERSTAND_PROVIDER", "groq")
        monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
        monkeypatch.setenv("GROQ_BASE_URL", server.url)
        get_settings.cache_clear()
        return understand_scenes([SCENE], SCENE.text)
    finally:
        server.close()


def test_a_fake_llm_card_with_21_percent_is_rejected(monkeypatch):
    meanings, _, warnings = run_groq(monkeypatch, {"headline": "21%", "subtitle": "household savings grew"})
    card = meanings[0].card
    assert card["headline"] == "40%" and "21" not in json.dumps(card)
    assert any("'21%'" in w and "narration does not say" in w for w in warnings)


def test_a_wrong_number_in_the_llm_subtitle_is_rejected_too(monkeypatch):
    meanings, _, warnings = run_groq(monkeypatch, {"headline": "40%", "subtitle": "up from 21% in 2020"})
    assert "21" not in json.dumps(meanings[0].card) and "2020" not in json.dumps(meanings[0].card)
    assert len(warnings) == 1


def test_a_checked_llm_subtitle_from_the_same_line_may_be_used(monkeypatch):
    meanings, _, warnings = run_groq(monkeypatch, {"headline": "40%", "subtitle": "savings grew"})
    assert warnings == []
    assert meanings[0].card["headline"] == "40%" and meanings[0].card["subtitle"] == "savings grew"


def test_an_llm_subtitle_with_words_not_in_the_line_is_not_used(monkeypatch):
    meanings, _, warnings = run_groq(monkeypatch, {"headline": "40%", "subtitle": "record breaking boom"})
    assert meanings[0].card["subtitle"] == "household savings grew" and warnings == []


# ---------- What is drawn and recorded ----------

def test_the_rendered_card_shows_only_the_final_number(tmp_path):
    """No count-up: the .ass file holds "40%" and no other number (the old animation drew 3%, 5% ... 37%)."""
    card = GeneratedProvider().card(0, "40%", "household savings grew")
    text = write_card_ass(card.extra, 1920, 1080, 4.0, tmp_path / "card.ass").read_text(encoding="utf-8")
    dialogue = [line for line in text.splitlines() if line.startswith("Dialogue:")]
    shown = re.sub(r"\{[^}]*\}", " ", " ".join(line.split(",,", 2)[-1] for line in dialogue))  # drop {\pos..}
    assert "40%" in shown and unverified_numbers(shown, SCENE.text) == []
    assert len(dialogue) == 2  # one headline + one subtitle line, nothing that changes over time


def test_card_scene_records_card_text_and_source_in_the_timeline(tmp_path, monkeypatch):
    monkeypatch.setenv("STATISTIC_OVERLAY", "false")  # the plain number card path
    get_settings.cache_clear()
    meanings = MockUnderstander().understand([SCENE], SCENE.text)
    plan = plan_search(meanings[0], SCENE.text)
    ctx = SelectionContext(work=tmp_path, orientation="landscape", warn=lambda m: None, scorer=get_scorer())
    sel = select_clip(SCENE, meanings[0], plan, ctx)
    facts = card_facts(sel)
    assert facts["card_text"] == "40%" and facts["card_source"] == "narration"
    assert facts["card_subtitle"] == "household savings grew" and facts["card_numbers"] == ["40%"]


def test_a_bad_card_is_never_drawn_even_if_it_reaches_the_ranker():
    """Defence in depth: a stored card with a number the narration does not say -> keyword card + warning."""
    warnings = []
    meaning = SceneMeaning(0, ["savings"], {}, "test", "mock", keyword="savings",
                           card={"headline": "21%", "subtitle": "household savings grew"})
    cand = _card(SCENE, meaning, warnings.append)
    assert cand.extra["headline"] == "SAVINGS" and "21" not in json.dumps(cand.extra)
    assert warnings and "21%" in warnings[0]


def test_stock_scenes_have_no_card_text():
    cand = GeneratedProvider().card(0, "x")
    cand.generated = False
    assert card_facts(Selection(0, cand, None, 0.3, 0.3, "stock"))["card_text"] is None


def test_x_in_y_ratio_never_reads_a_year_and_is_checked_like_out_of():
    assert [(f.kind, f.display) for f in find_figures("We moved 2 in 2025.")] == [("number", "2"), ("year", "2025")]
    assert find_figures("Call us at one in the morning.") == []
    line = "Last year, one in five people received a fake bank message."
    assert check_card("1 in 5", "", line) == [] and check_card("1 in 4", "", line) == ["1 in 4"]
