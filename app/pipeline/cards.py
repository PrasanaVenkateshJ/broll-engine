"""The text of a generated motion card, taken ONLY from the scene's own narration.

The rule (must-have 5): a card never shows a number or claim that is not in the narration.
- find_figures(): every number in a line, read by plain code (no LLM): "40%", "40 percent",
  "forty percent", "$1.2 million", "Rs 500", "3x", "three times", "2 million", "2025" ...
- main_figure(): the one to show. A percentage beats money, money beats "3x", "3x" beats a
  plain number, and a year comes last ("In 2025, savings grew by 40%" -> 40%).
- build_card(): headline = that figure (or a keyword if the line has no number) + a short
  subtitle phrase cut from the SAME sentence.
- unverified_numbers(): the guard. Every number in a text we are about to show must match a
  number in the narration; anything else (e.g. an LLM's "21%") is dropped with a warning.
"""
import re
from dataclasses import dataclass, field

# ---------- Reading numbers ----------

_ONES = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
         "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
         "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
         "ninety": 90}
_WORD = "|".join(list(_ONES) + list(_TENS) + ["hundred", "a"])
# "forty", "twenty-five", "one hundred and twenty", "a hundred"
_WORD_NUMBER = rf"(?:(?:{_WORD})(?:[\s-]+(?:and[\s-]+)?(?:{_WORD}))*)"
_DIGITS = r"\d+(?:,\d{3})*(?:\.\d+)?|\.\d+"
_SCALES = {"thousand": 1e3, "k": 1e3, "lakh": 1e5, "million": 1e6, "m": 1e6, "mn": 1e6, "crore": 1e7,
           "billion": 1e9, "bn": 1e9, "b": 1e9, "trillion": 1e12, "tn": 1e12}
_SCALE = r"thousand|million|billion|trillion|lakh|crore|bn|mn|tn|k|m|b"
_SHORT_SCALE = {1e3: "K", 1e6: "M", 1e9: "B", 1e12: "T"}  # "$1.2 million" -> "$1.2M"
_CURRENCY = r"[$€£₹¥]|rs\.?|inr|usd|eur|gbp"
_CURRENCY_WORDS = r"dollars?|euros?|pounds?|rupees?"

_NUM = rf"(?P<num>{_DIGITS}|{_WORD_NUMBER})"
_SCALE_PART = rf"(?:\s*(?P<scale>{_SCALE})\b)?"
PATTERNS = [  # (kind, regex) - tried in this order; a later match may not overlap an earlier one
    ("percent", re.compile(rf"\b{_NUM}\s*(?:%|\bper\s?cent\b)", re.I)),
    ("money", re.compile(rf"(?<![A-Za-z])(?P<cur>{_CURRENCY})\s?{_NUM}{_SCALE_PART}", re.I)),
    ("money", re.compile(rf"\b{_NUM}{_SCALE_PART}\s+(?P<curword>{_CURRENCY_WORDS})\b", re.I)),
    ("multiplier", re.compile(rf"\b{_NUM}\s*(?:x\b|times\b)", re.I)),
    ("scaled", re.compile(rf"\b(?P<num>{_DIGITS}|{_WORD_NUMBER})\s*(?P<scale>thousand|million|billion|trillion|"
                          rf"lakh|crore)\b", re.I)),
    ("number", re.compile(rf"(?<![\w.])(?P<num>{_DIGITS})(?![\w%])")),  # digits only: "one" is not a number
]
_CHANGE_WORDS = re.compile(r"\b(doubled|tripled|quadrupled|halved)\b", re.I)
PRIORITY = {"percent": 0, "money": 1, "multiplier": 2, "scaled": 3, "number": 4, "year": 5}


@dataclass
class Figure:
    kind: str       # percent | money | multiplier | scaled | number | year
    text: str       # the words as said, e.g. "forty percent"
    display: str    # what a card shows, e.g. "40%"
    value: float    # the number itself, scale applied: "$1.2 million" -> 1200000.0
    raw: float      # the number before the scale: "$1.2 million" -> 1.2
    start: int      # position in the line (ties: the first one wins)


def words_to_number(text: str) -> float | None:
    """ "forty" -> 40, "twenty-five" -> 25, "one hundred and five" -> 105, "a hundred" -> 100."""
    total, current, seen = 0, 0, False
    for word in re.split(r"[\s-]+", text.lower().strip()):
        if word in ("and", ""):
            continue
        if word == "a":
            current, seen = current or 1, True
        elif word in _ONES:
            current, seen = current + _ONES[word], True
        elif word in _TENS:
            current, seen = current + _TENS[word], True
        elif word == "hundred":
            current, seen = (current or 1) * 100, True
        else:
            return None
    return float(total + current) if seen else None


def _parse_number(text: str) -> float | None:
    text = text.strip()
    if re.fullmatch(_DIGITS, text):
        return float(text.replace(",", ""))
    return words_to_number(text)


def _plain(value: float) -> str:
    """40.0 -> "40", 1.25 -> "1.25", 12000 -> "12,000"."""
    if value == int(value):
        return f"{int(value):,}" if abs(value) >= 10000 else str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _is_year(text: str, value: float) -> bool:
    return bool(re.fullmatch(r"(1[5-9]|20)\d\d", text)) and value == int(value)


def find_figures(text: str) -> list[Figure]:
    """Every number in the line, in reading order. A bare word like "one" is NOT a number ("one honest
    decision"): number words count only with %, percent, a currency, a scale word or "times"."""
    found: list[Figure] = []
    taken: list[tuple[int, int]] = []
    for kind, pattern in PATTERNS:
        for m in pattern.finditer(text):
            if any(m.start() < end and start < m.end() for start, end in taken):
                continue  # already read as a stronger kind ("40%" is not also a plain 40)
            raw = _parse_number(m.group("num"))
            if raw is None or (m.group("num")[:1].isalpha() and kind == "number"):
                continue
            groups = m.groupdict()
            scale_word = (groups.get("scale") or "").lower()
            scale = _SCALES.get(scale_word, 1.0)
            if kind == "money" and scale_word in ("m", "b", "k") and not groups.get("cur"):
                continue
            value = raw * scale
            if kind == "percent":
                display = f"{_plain(raw)}%"
            elif kind == "money":
                cur = groups.get("cur") or ""
                cur = {"rs": "Rs ", "rs.": "Rs ", "inr": "Rs ", "usd": "$", "eur": "€", "gbp": "£"}.get(
                    cur.lower(), cur)
                if not cur:  # "500 dollars" -> "$500"
                    word = groups["curword"].lower()
                    cur = {"dol": "$", "eur": "€", "pou": "£", "rup": "Rs "}[word[:3]]
                display = f"{cur}{_plain(raw)}{_scale_suffix(scale, scale_word)}"
            elif kind == "multiplier":
                display = f"{_plain(raw)}x"
            elif kind == "scaled":
                display = f"{_plain(raw)}{_scale_suffix(scale, scale_word)}"
            elif _is_year(m.group("num"), raw):
                kind, display = "year", m.group("num")
            else:
                display = _plain(raw)
            found.append(Figure(kind, m.group(0).strip(), display, value, raw, m.start()))
            taken.append((m.start(), m.end()))
    return sorted(found, key=lambda f: f.start)


def _scale_suffix(scale: float, word: str) -> str:
    if scale == 1.0:
        return ""
    if scale in _SHORT_SCALE:
        return _SHORT_SCALE[scale]
    return f" {word}"  # lakh / crore stay words: "Rs 5 crore"


def main_figure(figures: list[Figure]) -> Figure | None:
    """The figure a card shows: by PRIORITY (a percentage first, a year last), then the first said."""
    return min(figures, key=lambda f: (PRIORITY[f.kind], f.start)) if figures else None


def change_word(text: str) -> Figure | None:
    """ "Prices doubled" -> DOUBLED (a claim word said in the line, not a number)."""
    m = _CHANGE_WORDS.search(text)
    return Figure("change", m.group(1), m.group(1).upper(), 0.0, 0.0, m.start()) if m else None


def _same_kind(shown: str, said: str) -> bool:
    """A percentage must match a percentage and money must match money; other numbers match any kind."""
    strict = {"percent", "money"}
    return shown == said or (shown not in strict and said not in strict)


# ---------- The guard ----------

def unverified_numbers(shown: str, narration: str) -> list[str]:
    """The numbers in `shown` that are NOT in the narration ([] = safe to show).
    "40%" matches "40 percent" / "forty percent"; "$1.2M" matches "$1.2 million"; "2025" matches "2025"."""
    said = find_figures(narration)
    bad = []
    for fig in find_figures(shown):
        if not any(abs(fig.value - f.value) < 1e-6 and _same_kind(fig.kind, f.kind) for f in said):
            bad.append(fig.text)
    # A digit the patterns could not read at all is never trusted.
    leftover = re.sub("|".join(re.escape(f.text) for f in find_figures(shown)) or r"(?!)", " ", shown)
    bad += re.findall(r"\d[\d.,]*", leftover)
    return bad


# ---------- The card ----------

_FILLER_END = {"by", "of", "to", "at", "in", "on", "for", "from", "about", "around", "nearly", "almost", "over",
               "under", "than", "the", "a", "an", "and", "or", "with", "up", "down", "is", "are", "was", "were",
               "just", "only", "more", "less", "some", "per"}
_FILLER_START = {"and", "but", "so", "then", "also", "that", "which", "while", "since", "in", "on", "by", "about",
                 "of", "the", "a", "an", "to", "for", "at", "with"}
MAX_SUBTITLE_WORDS = 5
# Part of every stored understanding's fingerprint: change it when these rules change, so an
# answer saved in an old timeline.json (with an old card) is not reused.
CARD_RULES_VERSION = "2"
MAX_SUBTITLE_CHARS = 40


@dataclass
class Card:
    headline: str                    # "40%", or a keyword like "TRUST" when the line has no number
    subtitle: str = ""               # "household savings grew" (words from the same sentence)
    number: str | None = None        # the figure shown, or None for a keyword card
    kind: str = "keyword"            # the figure's kind, or "keyword" / "change"
    source: str = "narration"        # where the text came from: always the narration
    numbers_shown: list[str] = field(default_factory=list)  # every number on the card (all verified)

    def to_dict(self) -> dict:
        return {"headline": self.headline, "subtitle": self.subtitle, "number": self.number, "kind": self.kind,
                "source": self.source, "numbers_shown": self.numbers_shown}

    @classmethod
    def from_dict(cls, data: dict) -> "Card":
        return cls(**{k: data[k] for k in ("headline", "subtitle", "number", "kind", "source", "numbers_shown")
                      if k in data})


def _sentence_with(text: str, position: int) -> str:
    start = 0
    for part in re.split(r"(?<=[.!?])\s+", text):
        end = start + len(part)
        if start <= position <= end:
            return part
        start = end + 1
    return text


def _clean(part: str) -> list[str]:
    """Words of a phrase without filler at its start/end, at most MAX_SUBTITLE_WORDS / MAX_SUBTITLE_CHARS."""
    words = [w.strip(".,!?\"'") for w in part.split()]
    words = [w for w in words if w]
    while words and words[0].lower() in _FILLER_START:
        words.pop(0)
    words = words[:MAX_SUBTITLE_WORDS]
    while words and words[-1].lower() in _FILLER_END:
        words.pop()
    while len(" ".join(words)) > MAX_SUBTITLE_CHARS and len(words) > 1:
        words.pop()
    return words


def subtitle_phrase(text: str, figure: Figure | None, names: list[str] | tuple = ()) -> str:
    """A short phrase cut from the SAME sentence as the figure, without the figure itself:
    "In 2025, household savings grew by 40%." -> "household savings grew".
    Tried in order: the words before the figure in its clause, the words after it, both, then the
    longest other clause of the sentence ("In 2025, humans ..." -> "humans ..."). A phrase needs
    at least 2 real words (3+ letters)."""
    sentence = _sentence_with(text, figure.start) if figure else re.split(r"(?<=[.!?])\s+", text)[0]
    clauses = [c for c in re.split(r"[,;:—–]|\s-\s", sentence) if c.strip()]
    if figure and figure.kind != "change":
        own = next((c for c in clauses if figure.text in c), sentence)
        before, _, after = own.partition(figure.text)
        others = sorted((c for c in clauses if c is not own), key=len, reverse=True)
        options = [before, after, f"{before} {after}", *others]
    else:  # a claim word ("doubled") stays in its phrase: "prices doubled in a decade"
        options = [next((c for c in clauses if figure and figure.text in c), clauses[0] if clauses else sentence)]
    name_words = {w.lower() for n in names for w in n.split()}
    words: list[str] = []
    for option in options:
        words = _clean(option)
        if any(w.lower() in name_words for w in words):
            continue  # never a person's or brand's name on a card
        if sum(len(w) >= 3 and any(ch.isalpha() for ch in w) for w in words) >= 2:
            break
    else:
        words = []
    if words and words[0][1:].islower() and words[0] == sentence.split(" ", 1)[0].strip(".,"):
        words[0] = words[0].lower()  # "Sales grew" -> "sales grew" (only the capital of the sentence start)
    return " ".join(words)


def build_card(text: str, keyword: str = "", names: list[str] | tuple = ()) -> Card:
    """The card for one scene, built ONLY from its narration. keyword: the label for a line without a number."""
    figure = main_figure(find_figures(text))
    if figure:
        card = Card(headline=figure.display, subtitle=subtitle_phrase(text, figure, names), number=figure.display,
                    kind=figure.kind)
    elif change := change_word(text):
        card = Card(headline=change.display, subtitle=subtitle_phrase(text, change, names), kind="change")
    else:
        card = Card(headline=(keyword or "keep watching").upper(), kind="keyword")
    if unverified_numbers(card.subtitle, text):  # cannot happen by construction; the guard still decides
        card.subtitle = ""
    card.numbers_shown = [f.display for f in find_figures(f"{card.headline} {card.subtitle}")]
    return card


def check_card(headline: str, subtitle: str, narration: str) -> list[str]:
    """Last check right before a card is drawn: the numbers on it that the narration does not say."""
    return unverified_numbers(f"{headline} {subtitle}", narration)
