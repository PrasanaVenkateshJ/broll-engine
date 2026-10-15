"""Cut the narration into scenes (one B-roll clip per scene).

The narration sets the timing. The rules (from CLAUDE.md):
- Scenes are contiguous: scene 0 starts at 0, each scene starts where the previous one
  ended, the last one ends at the end of the audio. No gaps, no overlaps.
- Cut first at sentence ends, then at commas / phrase pauses, and only if nothing else
  works, between two plain words.
- No scene shorter than MIN_SCENE_SECONDS (merge it), none longer than MAX_SCENE_SECONDS (split it).

The algorithm in 3 steps:
1. Split the words into sentences.
2. A sentence longer than MAX is cut at its best inside point: the strongest boundary
   (comma/pause beats a plain word gap), and among equals the one nearest the middle.
   Repeat until every piece fits.
3. A piece shorter than MIN is joined to the neighbour that gives the shorter result
   (if that is now too long, step 2 cuts it again somewhere better).
The cut time between two words is the middle of the gap between them.
"""
import re
from dataclasses import dataclass, field

from app.config import get_settings
from app.pipeline.transcribe import Word

SENTENCE_END = re.compile(r"[.!?…]+[\"'”’)\]]*$")
CLAUSE_END = re.compile(r"[,;:—–][\"'”’)\]]*$")
ABBREVIATIONS = {"mr.", "mrs.", "ms.", "dr.", "prof.", "st.", "jr.", "sr.", "vs.", "etc.", "e.g.", "i.e."}

# How good a place is to cut (after a word):
SENTENCE, CLAUSE, PAUSE, PLAIN = 3, 2, 1, 0


@dataclass
class Scene:
    index: int
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)
    visual_hint: str | None = None

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)

    def to_dict(self) -> dict:
        return {"index": self.index, "start": self.start, "end": self.end, "duration": self.duration,
                "text": self.text, "visual_hint": self.visual_hint,
                "words": [{"text": w.text, "start": w.start, "end": w.end} for w in self.words]}


def boundary_strength(word: Word, next_word: Word, pause_seconds: float) -> int:
    """How strong is the boundary right after `word`?"""
    text = word.text.lower()
    if SENTENCE_END.search(text) and text not in ABBREVIATIONS:
        return SENTENCE
    if CLAUSE_END.search(text):
        return CLAUSE
    if next_word.start - word.end >= pause_seconds:
        return PAUSE
    return PLAIN


def split_into_scenes(words: list[Word], audio_duration: float, min_seconds: float | None = None,
                      max_seconds: float | None = None, pause_seconds: float | None = None) -> list[Scene]:
    s = get_settings()
    min_s = s.MIN_SCENE_SECONDS if min_seconds is None else min_seconds
    max_s = s.MAX_SCENE_SECONDS if max_seconds is None else max_seconds
    pause = s.PAUSE_SECONDS if pause_seconds is None else pause_seconds
    audio_end = round(max(audio_duration, words[-1].end if words else 0.0), 3)
    if not words:
        return [Scene(0, 0.0, audio_end, "")]

    n = len(words)
    # cuts[i] = the time we would cut at if we cut right after word i.
    cuts = [round((words[i].end + words[i + 1].start) / 2, 3) for i in range(n - 1)]
    strength = [boundary_strength(words[i], words[i + 1], pause) for i in range(n - 1)]

    # A piece = (first word index, last word index), both included.
    def start_of(a: int) -> float:
        return 0.0 if a == 0 else cuts[a - 1]

    def end_of(b: int) -> float:
        return audio_end if b == n - 1 else cuts[b]

    def length(a: int, b: int) -> float:
        return end_of(b) - start_of(a)

    def best_cut(a: int, b: int) -> int:
        """Where to cut the piece a..b: strongest boundary, then nearest the middle.
        Only places that leave both halves >= MIN, unless there are none."""
        middle = (start_of(a) + end_of(b)) / 2
        places = [i for i in range(a, b) if length(a, i) >= min_s and length(i + 1, b) >= min_s]
        places = places or list(range(a, b))
        return max(places, key=lambda i: (strength[i], -abs(cuts[i] - middle)))

    def split_long(a: int, b: int) -> list[tuple[int, int]]:
        if a == b or length(a, b) <= max_s:
            return [(a, b)]
        i = best_cut(a, b)
        return split_long(a, i) + split_long(i + 1, b)

    # Step 1: sentences.
    pieces, first = [], 0
    for i in range(n - 1):
        if strength[i] == SENTENCE:
            pieces.append((first, i))
            first = i + 1
    pieces.append((first, n - 1))

    # Step 2: cut long sentences.
    pieces = [part for a, b in pieces for part in split_long(a, b)]

    # Step 3: join short pieces to a neighbour. (The guard stops a loop in impossible cases.)
    for _ in range(4 * n):
        short = [j for j, (a, b) in enumerate(pieces) if length(a, b) < min_s]
        if not short or len(pieces) == 1:
            break
        j = min(short, key=lambda k: length(*pieces[k]))
        neighbours = [k for k in (j - 1, j + 1) if 0 <= k < len(pieces)]
        k = min(neighbours, key=lambda k: length(min(pieces[j][0], pieces[k][0]), max(pieces[j][1], pieces[k][1])))
        lo, hi = min(j, k), max(j, k)
        merged = (pieces[lo][0], pieces[hi][1])
        pieces[lo:hi + 1] = split_long(*merged)

    return [Scene(index=idx, start=start_of(a), end=end_of(b), text=" ".join(w.text for w in words[a:b + 1]),
                  words=words[a:b + 1]) for idx, (a, b) in enumerate(pieces)]


def scenes_from_list(items: list[dict], durations: list[float], words: list[Word],
                     audio_duration: float) -> list[Scene]:
    """Scene-list input: one scene per item, timed by the length of its own narration.

    `durations[i]` = length of the TTS audio for item i. The last scene ends exactly at
    audio_duration. Words go to the scene that holds the middle of the word (for captions).
    """
    scenes, t = [], 0.0
    for i, (item, seconds) in enumerate(zip(items, durations)):
        start = round(t, 3)
        end = round(audio_duration, 3) if i == len(items) - 1 else round(t + seconds, 3)
        scene_words = [w for w in words if start <= (w.start + w.end) / 2 < end
                       or (i == len(items) - 1 and (w.start + w.end) / 2 >= end)]
        scenes.append(Scene(index=i, start=start, end=end, text=item["text"].strip(), words=scene_words,
                            visual_hint=item.get("visual_hint")))
        t = end
    return scenes
