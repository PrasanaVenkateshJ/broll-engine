"""captions.srt: valid SRT format, max 2 lines of ~42 characters, timed from the words."""
import re

from app.pipeline.outputs import balanced, build_cues, srt_time, to_srt, wrap
from app.pipeline.scenes import split_into_scenes
from tests.unit.test_scenes import words_from

SRT_BLOCK = re.compile(r"^(\d+)\n(\d\d:\d\d:\d\d,\d{3}) --> (\d\d:\d\d:\d\d,\d{3})\n(.+(?:\n.+)?)$")

SCRIPT = ("Saving money is not about earning more. It is about spending wisely. In 2025, household savings "
          "grew by 40 percent. Cloud computing now lets small businesses work from anywhere. Trust is earned "
          "slowly, one honest decision at a time.")


def seconds(stamp: str) -> float:
    h, m, rest = stamp.split(":")
    s, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def make_cues(text=SCRIPT):
    words = words_from(text)
    scenes = split_into_scenes(words, words[-1].end + 0.5)
    return scenes, build_cues(scenes)


def test_srt_time_format():
    assert srt_time(0) == "00:00:00,000"
    assert srt_time(12.5) == "00:00:12,500"
    assert srt_time(3661.007) == "01:01:01,007"


def test_srt_file_format_and_limits():
    _, cues = make_cues()
    srt = to_srt(cues)
    blocks = srt.strip("\n").split("\n\n")
    assert len(blocks) == len(cues) > 1
    last_end = 0.0
    for number, block in enumerate(blocks, start=1):
        match = SRT_BLOCK.match(block)
        assert match, block
        assert int(match.group(1)) == number  # numbered 1, 2, 3 ...
        start, end = seconds(match.group(2)), seconds(match.group(3))
        assert last_end <= start < end  # in order, never overlapping
        last_end = end
        lines = match.group(4).split("\n")
        assert 1 <= len(lines) <= 2
        assert all(len(line) <= 42 for line in lines)


def test_captions_contain_every_word_once_in_order():
    _, cues = make_cues()
    caption_words = " ".join(" ".join(c.lines) for c in cues).split()
    assert caption_words == SCRIPT.split()


def test_cues_follow_the_word_times_and_stay_inside_their_scene():
    scenes, cues = make_cues()
    for cue in cues:
        scene = scenes[cue.scene_index]
        assert scene.start <= cue.start < cue.end <= scene.end
        first_word = next(w for w in scene.words if w.text == cue.lines[0].split()[0])
        assert abs(cue.start - first_word.start) < 0.5


def test_new_caption_after_each_sentence():
    _, cues = make_cues("Short one. Another short one.")
    assert [c.text for c in cues] == ["Short one.", "Another short one."]


def test_line_wrapping_helpers():
    tokens = "a fairly long caption line that will certainly need to wrap onto two lines".split()
    lines = balanced(tokens, 42)
    assert len(lines) == 2 and all(len(line) <= 42 for line in lines)
    assert abs(len(lines[0]) - len(lines[1])) <= 10  # balanced, not one long + one tiny
    assert wrap(["supercalifragilisticexpialidocious" * 2], 42) == ["supercalifragilisticexpialidocious" * 2]


def test_footage_mix_counts_video_photo_and_cards():
    from app.pipeline.outputs import footage_mix
    from app.pipeline.providers.base import Candidate
    from app.pipeline.rank import Selection

    def sel(i, kind, generated=False, reason="mock: score 0.300"):
        c = Candidate(provider="generated" if generated else "x", id=str(i), kind=kind, generated=generated)
        return Selection(i, c, None, None, None, reason)

    mix = footage_mix([sel(0, "video"), sel(1, "video"), sel(2, "image"),
                       sel(3, "generated", True, "statistic line: animated number card '40%'"),
                       sel(4, "generated", True, "no stock source passed the rule")])
    assert mix["counts"] == {"video": 2, "photo_motion": 1, "generated_card": 2}
    assert mix["share"] == {"video": 0.4, "photo_motion": 0.2, "generated_card": 0.4}
    assert mix["statistic_cards"] == 1  # the number card is on purpose, so the fair share leaves it out
    assert mix["share_without_statistic_cards"] == {"video": 0.5, "photo_motion": 0.25, "generated_card": 0.25}
    assert footage_mix([])["share"] == {"video": 0.0, "photo_motion": 0.0, "generated_card": 0.0}
