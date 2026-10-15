"""Scene splitting rules: contiguous, sentence ends first, then commas/pauses, MIN <= length <= MAX."""
import random

from app.pipeline.scenes import CLAUSE, PAUSE, PLAIN, SENTENCE, boundary_strength, scenes_from_list, split_into_scenes
from app.pipeline.transcribe import Word

MIN, MAX = 2.0, 8.0


def words_from(text: str, wps: float = 2.5, start: float = 0.0, sentence_pause: float = 0.3) -> list[Word]:
    """Fake word timings: `wps` words per second, a short pause after each sentence."""
    words, t = [], start
    for token in text.split():
        words.append(Word(token, round(t, 3), round(t + 0.8 / wps, 3)))
        t += 1 / wps
        if token.endswith((".", "!", "?")):
            t += sentence_pause
    return words


def assert_contiguous(scenes, audio_end):
    assert scenes[0].start == 0.0
    for prev, nxt in zip(scenes, scenes[1:]):
        assert nxt.start == prev.end  # no gap, no overlap
    assert scenes[-1].end == round(audio_end, 3)
    assert all(sc.duration > 0 for sc in scenes)
    assert [sc.index for sc in scenes] == list(range(len(scenes)))


def assert_lengths_ok(scenes):
    for sc in scenes:
        assert MIN <= sc.duration <= MAX, (sc.index, sc.duration, sc.text)


def test_boundary_strengths():
    nxt = Word("next", 10.0, 10.2)
    assert boundary_strength(Word("end.", 0, 1), nxt, 0.35) == SENTENCE
    assert boundary_strength(Word("really?", 0, 1), nxt, 0.35) == SENTENCE
    assert boundary_strength(Word("Dr.", 0, 1), Word("Smith", 1.05, 1.3), 0.35) == PLAIN  # abbreviation
    assert boundary_strength(Word("first,", 0, 1), Word("then", 1.05, 1.3), 0.35) == CLAUSE
    assert boundary_strength(Word("wait", 0, 1), Word("then", 1.5, 1.8), 0.35) == PAUSE
    assert boundary_strength(Word("plain", 0, 1), Word("then", 1.05, 1.3), 0.35) == PLAIN


def test_two_normal_sentences_become_two_scenes_cut_in_the_pause():
    words = words_from("This first sentence takes about four seconds to say. "
                       "And this second sentence also takes about four seconds.")
    end = words[-1].end + 0.3
    scenes = split_into_scenes(words, end, MIN, MAX)
    assert_contiguous(scenes, end)
    assert [sc.text.split()[-1] for sc in scenes] == ["say.", "seconds."]
    first_end_word, second_start_word = scenes[0].words[-1], scenes[1].words[0]
    assert first_end_word.end < scenes[0].end < second_start_word.start  # the cut sits in the pause


def test_long_sentence_without_punctuation_is_split_below_max():
    words = words_from(" ".join(["word"] * 60))  # one 24-second "sentence", no commas at all
    end = words[-1].end
    scenes = split_into_scenes(words, end, MIN, MAX)
    assert_contiguous(scenes, end)
    assert_lengths_ok(scenes)
    assert len(scenes) >= 3


def test_long_sentence_is_cut_at_the_comma_not_a_random_word():
    text = " ".join(["one"] * 14) + " fourteen, " + " ".join(["two"] * 14) + " end."
    words = words_from(text)  # ~12 s, one comma in the middle
    scenes = split_into_scenes(words, words[-1].end, MIN, MAX)
    assert len(scenes) == 2
    assert scenes[0].words[-1].text == "fourteen,"


def test_very_short_sentence_is_merged():
    words = words_from("This is a normal sentence that lasts a few seconds. Hi. "
                       "Here is another sentence of normal length for us.")
    end = words[-1].end
    scenes = split_into_scenes(words, end, MIN, MAX)
    assert_contiguous(scenes, end)
    assert_lengths_ok(scenes)
    assert len(scenes) == 2
    assert all(sc.text.endswith(".") for sc in scenes)  # merging kept the cuts at sentence ends
    assert any("Hi." in sc.text.split() for sc in scenes)


def test_many_short_sentences_are_grouped():
    words = words_from("Yes. No. Maybe. Sure. Okay. Fine. Right. Good. Great. Done. Again. Stop.")
    end = words[-1].end
    scenes = split_into_scenes(words, end, MIN, MAX)
    assert_contiguous(scenes, end)
    assert_lengths_ok(scenes)
    assert 1 < len(scenes) < 12
    assert all(sc.text.endswith(".") for sc in scenes)


def test_audio_shorter_than_min_gives_one_scene():
    words = words_from("Hi.", start=0.2)
    scenes = split_into_scenes(words, 1.0, MIN, MAX)
    assert len(scenes) == 1
    assert (scenes[0].start, scenes[0].end) == (0.0, 1.0)


def test_no_words_gives_one_scene_over_the_whole_audio():
    scenes = split_into_scenes([], 5.0, MIN, MAX)
    assert len(scenes) == 1 and scenes[0].end == 5.0


def test_no_gaps_property_on_many_random_inputs():
    rng = random.Random(42)  # fixed seed: the same "random" inputs every run
    vocab = ["cloud", "money", "trust", "data", "people", "grow", "every", "day", "small", "work"]
    for _ in range(200):
        tokens = []
        for _ in range(rng.randint(1, 120)):
            word = rng.choice(vocab)
            roll = rng.random()
            word += "." if roll < 0.08 else "," if roll < 0.15 else ""
            tokens.append(word)
        lead, tail = rng.uniform(0, 1.5), rng.uniform(0, 2.0)  # silence before and after the speech
        words = words_from(" ".join(tokens), wps=rng.uniform(1.8, 3.5), start=lead)
        end = words[-1].end + tail
        scenes = split_into_scenes(words, end, MIN, MAX)
        assert_contiguous(scenes, end)
        assert sum(len(sc.words) for sc in scenes) == len(words)  # every word in exactly one scene
        if end >= MIN:
            assert_lengths_ok(scenes)


def test_scene_list_one_scene_per_item_keeps_hints():
    items = [{"text": "Money grows slowly.", "visual_hint": "coins in a jar"},
             {"text": "Trust takes time.", "visual_hint": None},
             {"text": "Start today."}]
    durations = [2.5, 3.0, 1.5]
    words = (words_from("Money grows slowly.", start=0.1) + words_from("Trust takes time.", start=2.6)
             + words_from("Start today.", start=5.6))
    scenes = scenes_from_list(items, durations, words, audio_duration=7.0)
    assert_contiguous(scenes, 7.0)
    assert [sc.visual_hint for sc in scenes] == ["coins in a jar", None, None]
    assert [sc.text for sc in scenes] == [i["text"] for i in items]
    assert [w.text for w in scenes[1].words] == ["Trust", "takes", "time."]
