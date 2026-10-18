"""The text of a generated motion card, taken ONLY from the scene's own narration.

The rule (must-have 5): a card never shows a number or claim that is not in the narration.
- find_figures(): every number in a line, read by plain code (no LLM): "40%", "40 percent",
  "three out of four", "twelve kilometres", Hindi "दस प्रतिशत", Tamil "பத்து சதவீதம்",
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
           "billion": 1e9, "bn": 1e9, "b": 1e9, "trillion": 1e12, "tn": 1e12, "t": 1e12}
_SCALE = r"thousand|million|billion|trillion|lakh|crore|bn|mn|tn|k|m|b"
_SHORT_SCALE = {1e3: "K", 1e6: "M", 1e9: "B", 1e12: "T"}  # "$1.2 million" -> "$1.2M"
_CURRENCY = r"[$€£₹¥]|rs\.?|inr|usd|eur|gbp"
_CURRENCY_WORDS = r"dollars?|euros?|pounds?|rupees?"

_NUM = rf"(?P<num>{_DIGITS}|{_WORD_NUMBER})"
_SCALE_PART = rf"(?:\s*(?P<scale>{_SCALE})\b)?"
# Units of measure: "twelve kilometres" -> "12 km". Short form shown on the card.
UNITS = {"km": "km", "kms": "km", "kilometre": "km", "kilometres": "km", "kilometer": "km", "kilometers": "km",
         "mile": "miles", "miles": "miles", "metre": "m", "metres": "m", "meter": "m", "meters": "m",
         "cm": "cm", "centimetres": "cm", "centimeters": "cm", "kg": "kg", "kgs": "kg", "kilo": "kg", "kilos": "kg",
         "kilogram": "kg", "kilograms": "kg", "gram": "g", "grams": "g", "tonne": "tonnes", "tonnes": "tonnes",
         "ton": "tons", "tons": "tons", "litre": "litres", "litres": "litres", "liter": "litres", "liters": "litres",
         "acre": "acres", "acres": "acres", "hectare": "hectares", "hectares": "hectares", "degree": "degrees",
         "degrees": "degrees", "second": "seconds", "seconds": "seconds", "minute": "minutes", "minutes": "minutes",
         "hour": "hours", "hours": "hours", "day": "days", "days": "days", "week": "weeks", "weeks": "weeks",
         "month": "months", "months": "months", "year": "years", "years": "years"}
TIME_UNITS = {"seconds", "minutes", "hours", "days", "weeks", "months", "years"}
_UNIT = "|".join(sorted(UNITS, key=len, reverse=True))
PATTERNS = [  # (kind, regex) - tried in this order; a later match may not overlap an earlier one
    ("percent", re.compile(rf"\b{_NUM}\s*(?:%|\bper\s?cent\b)", re.I)),
    ("money", re.compile(rf"(?<![A-Za-z])(?P<cur>{_CURRENCY})\s?{_NUM}{_SCALE_PART}", re.I)),
    ("money", re.compile(rf"\b{_NUM}{_SCALE_PART}\s+(?P<curword>{_CURRENCY_WORDS})\b", re.I)),
    # "three out of four buyers" -> "3 out of 4"
    ("ratio", re.compile(rf"\b(?P<num>{_DIGITS}|{_WORD_NUMBER})\s+out\s+of\s+(?P<den>{_DIGITS}|{_WORD_NUMBER})\b",
                         re.I)),
    ("multiplier", re.compile(rf"\b{_NUM}\s*(?:x\b|times\b)", re.I)),
    ("scaled", re.compile(rf"\b(?P<num>{_DIGITS}|{_WORD_NUMBER})\s*(?P<scale>thousand|million|billion|trillion|"
                          rf"lakh|crore)\b", re.I)),
    ("measure", re.compile(rf"\b(?P<num>{_DIGITS}|{_WORD_NUMBER})[\s-]*(?P<unit>{_UNIT})\b", re.I)),
    # What a card itself shows for a scaled figure ("9K", "2M"), so the guard can read the card back.
    ("scaled", re.compile(rf"(?<![\w.])(?P<num>{_DIGITS})(?P<scale>[KMBT])(?![\w])")),
    ("number", re.compile(rf"(?<![\w.])(?P<num>{_DIGITS})(?![\w%])")),  # digits only: "one" is not a number
]
_CHANGE_WORDS = re.compile(r"\b(doubled|tripled|quadrupled|halved)\b", re.I)
PRIORITY = {"percent": 0, "money": 1, "ratio": 2, "multiplier": 3, "scaled": 4, "measure": 5, "number": 6, "year": 7}


# ---------- Hindi and Tamil number words (read from the spoken words, never from a translation) ----------

_HI_1_TO_100 = (
    "एक दो तीन चार पाँच छह सात आठ नौ दस ग्यारह बारह तेरह चौदह पंद्रह सोलह सत्रह अठारह उन्नीस बीस इक्कीस बाईस तेईस "
    "चौबीस पच्चीस छब्बीस सत्ताईस अट्ठाईस उनतीस तीस इकतीस बत्तीस तैंतीस चौंतीस पैंतीस छत्तीस सैंतीस अड़तीस उनतालीस "
    "चालीस इकतालीस बयालीस तैंतालीस चवालीस पैंतालीस छियालीस सैंतालीस अड़तालीस उनचास पचास इक्यावन बावन तिरपन चौवन "
    "पचपन छप्पन सत्तावन अट्ठावन उनसठ साठ इकसठ बासठ तिरसठ चौंसठ पैंसठ छियासठ सड़सठ अड़सठ उनहत्तर सत्तर इकहत्तर "
    "बहत्तर तिहत्तर चौहत्तर पचहत्तर छिहत्तर सतहत्तर अठहत्तर उन्यासी अस्सी इक्यासी बयासी तिरासी चौरासी पचासी छियासी "
    "सत्तासी अट्ठासी नवासी नब्बे इक्यानवे बानवे तिरानवे चौरानवे पंचानवे छियानवे सत्तानवे अट्ठानवे निन्यानवे सौ").split()
HINDI_NUMBERS: dict[str, float] = {w: float(i) for i, w in enumerate(_HI_1_TO_100, start=1)}
HINDI_NUMBERS.update({"पांच": 5, "छः": 6, "छ": 6, "छे": 6, "सत्ताइस": 27, "अट्ठाइस": 28, "चव्वालीस": 44,
                      "शून्य": 0, "डेढ़": 1.5, "ढाई": 2.5})
HINDI_SCALES = {"सौ": 100.0, "हज़ार": 1e3, "हजार": 1e3, "लाख": 1e5, "करोड़": 1e7, "करोड": 1e7}
HINDI_PERCENT = ("प्रतिशत", "फ़ीसदी", "फीसदी", "फ़ीसद", "फीसद", "परसेंट", "पर्सेंट")
HINDI_MONEY = ("रुपये", "रुपए", "रुपया", "रूपये", "रूपए")

_TA_UNITS = {"ஒன்று": 1, "இரண்டு": 2, "மூன்று": 3, "நான்கு": 4, "ஐந்து": 5, "ஆறு": 6, "ஏழு": 7, "எட்டு": 8,
             "ஒன்பது": 9}
_TA_TENS = {"இருபத்": 20, "முப்பத்": 30, "நாற்பத்": 40, "ஐம்பத்": 50, "அறுபத்": 60, "எழுபத்": 70, "எண்பத்": 80,
            "தொண்ணூற்": 90}
_TA_ROUND_TENS = {"பத்து": 10, "இருபது": 20, "முப்பது": 30, "நாற்பது": 40, "ஐம்பது": 50, "அறுபது": 60,
                  "எழுபது": 70, "எண்பது": 80, "தொண்ணூறு": 90, "நூறு": 100}
_TA_TEENS = {"பதினொன்று": 11, "பதினொரு": 11, "பன்னிரண்டு": 12, "பன்னிரெண்டு": 12, "பதின்மூன்று": 13,
             "பதிமூன்று": 13, "பதினான்கு": 14, "பதினைந்து": 15, "பதினாறு": 16, "பதினேழு": 17, "பதினெட்டு": 18,
             "பத்தொன்பது": 19}
# Tamil joins a vowel-initial word to the "-த்" before it: இருபத் + ஐந்து -> இருபத்தைந்து (pulli -> vowel sign).
_TA_VOWEL_SIGNS = {"அ": "", "ஆ": "ா", "இ": "ி", "ஈ": "ீ", "உ": "ு", "ஊ": "ூ", "எ": "ெ", "ஏ": "ே", "ஐ": "ை",
                   "ஒ": "ொ", "ஓ": "ோ"}


def _tamil_numbers() -> dict[str, float]:
    words: dict[str, float] = {**_TA_UNITS, **_TA_ROUND_TENS, **_TA_TEENS, "ஒரு": 1, "பூஜ்ஜியம்": 0}
    for stem, tens in _TA_TENS.items():
        joined = stem + stem[-2]  # the consonant doubles: "இருபத்" -> "இருபத்த" + vowel sign
        for unit, value in _TA_UNITS.items():
            if unit[0] in _TA_VOWEL_SIGNS:
                words[joined + _TA_VOWEL_SIGNS[unit[0]] + unit[1:]] = tens + value   # இருபத்தைந்து
            words[stem + "து" + unit] = tens + value                                  # இருபத்துமூன்று
        words[stem + "து"] = tens  # first word of a spaced compound: "இருபத்து மூன்று" (the unit is added)
        words[stem + "தி"] = tens
    return words


TAMIL_NUMBERS = _tamil_numbers()
TAMIL_SCALES = {"நூறு": 100.0, "ஆயிரம்": 1e3, "ஆயிரத்து": 1e3, "லட்சம்": 1e5, "லட்ச": 1e5, "கோடி": 1e7}
TAMIL_PERCENT = ("சதவீத", "சதவிகித", "விழுக்காடு", "பர்சென்ட்", "சதம்")
TAMIL_MONEY = ("ரூபாய்",)
INDIC_UNITS = {"किलोमीटर": "km", "किमी": "km", "मीटर": "m", "किलो": "kg", "किलोग्राम": "kg", "लीटर": "litres",
               "கிலோமீட்டர்": "km", "கி.மீ": "km", "மீட்டர்": "m", "கிலோ": "kg", "லிட்டர்": "litres"}
_INDIC_DIGITS = str.maketrans("०१२३४५६७८९௦௧௨௩௪௫௬௭௮௯", "01234567890123456789")
_TOKEN_PUNCT = ".,;:!?\"'()“”‘’।॥"


def _indic_value(word: str) -> tuple[float, str] | None:
    """(value, "number" | "scale") of one Hindi / Tamil / digit token, or None."""
    digits = word.translate(_INDIC_DIGITS).replace(",", "")
    if re.fullmatch(r"\d+(?:\.\d+)?", digits):
        return float(digits), "number"
    if word in HINDI_SCALES or word in TAMIL_SCALES:
        return HINDI_SCALES.get(word) or TAMIL_SCALES[word], "scale"
    if word in HINDI_NUMBERS:
        return HINDI_NUMBERS[word], "number"
    if word in TAMIL_NUMBERS:
        return TAMIL_NUMBERS[word], "number"
    return None


def _indic_marker(word: str) -> tuple[str, str] | None:
    """What a token after a number says: ("percent"|"money"|"unit", unit short form) - Tamil words may
    carry case endings (சதவீதத்தை), so Tamil markers match as a prefix."""
    if word in HINDI_PERCENT or word == "%" or word.startswith(TAMIL_PERCENT):
        return "percent", ""
    if word in HINDI_MONEY or word.startswith(TAMIL_MONEY):
        return "money", ""
    for unit, short in INDIC_UNITS.items():
        if word == unit or (unit[0] >= "஀" and word.startswith(unit)):
            return "unit", short
    return None


def _has_indic(text: str) -> bool:
    return any("ऀ" <= ch <= "ॿ" or "஀" <= ch <= "௿" for ch in text)


def indic_figures(text: str) -> list["Figure"]:
    """Hindi / Tamil figures: a run of number words (or digits) + percent / money / unit / scale word.
    "दस प्रतिशत" -> 10%, "पाँच लाख रुपये" -> Rs 5 lakh, "பத்து சதவீதம்" -> 10%, "இருபத்தைந்து கிலோமீட்டர்" -> 25 km.
    A number word alone is not a figure (the same rule as English "one")."""
    if not _has_indic(text):
        return []
    tokens = [(m.start(), m.end(), m.group().strip(_TOKEN_PUNCT)) for m in re.finditer(r"\S+", text)]
    found, i = [], 0
    while i < len(tokens):
        total, current, j, seen = 0.0, 0.0, i, False
        while j < len(tokens) and (got := _indic_value(tokens[j][2])) is not None:
            value, what = got
            if what == "scale":
                if value == 100:
                    current = (current or 1) * 100
                else:
                    total, current = total + (current or 1) * value, 0.0
            else:
                current += value  # "இருபத்து மூன்று": 20 + 3
            seen, j = True, j + 1
            if text[tokens[j - 1][1] - 1] in _TOKEN_PUNCT:  # "दस, ..." - a comma or full stop ends the number
                break
        if not seen:
            i += 1
            continue
        value = total + current
        scaled = total >= 1e3 and current == 0
        marker = _indic_marker(tokens[j][2]) if j < len(tokens) else None
        start, end = tokens[i][0], tokens[j][1] if marker else tokens[j - 1][1]
        biggest = max((v for _, _, t in tokens[i:j] for v, w in [_indic_value(t)] if w == "scale"), default=1.0)
        raw = value / biggest if scaled else value
        suffix = _scale_suffix(biggest, {1e5: "lakh", 1e7: "crore"}.get(biggest, "")) if scaled else ""
        if marker and marker[0] == "percent":
            fig = Figure("percent", text[start:end], f"{_plain(value)}%", value, value, start)
        elif marker and marker[0] == "money":
            fig = Figure("money", text[start:end], f"Rs {_plain(raw)}{suffix}", value, raw, start)
        elif marker and marker[0] == "unit":
            fig = Figure("measure", text[start:end], f"{_plain(value)} {marker[1]}", value, value, start)
        elif scaled:
            fig = Figure("scaled", text[start:end], f"{_plain(raw)}{suffix}", value, raw, start)
        else:
            i = j
            continue  # a bare number word: not a figure (digits are read by the main patterns)
        found.append(fig)
        i = j + (1 if marker else 0)
    return found


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
    found: list[Figure] = indic_figures(text)
    taken: list[tuple[int, int]] = [(f.start, f.start + len(f.text)) for f in found]
    for kind, pattern in PATTERNS:
        for m in pattern.finditer(text):
            if any(m.start() < end and start < m.end() for start, end in taken):
                continue  # already read as a stronger kind ("40%" is not also a plain 40)
            raw = _parse_number(m.group("num"))
            if raw is None or (m.group("num")[:1].isalpha() and kind == "number"):
                continue
            groups = m.groupdict()
            if kind == "ratio":
                den = _parse_number(groups["den"])
                if not den or raw > den:
                    continue
                found.append(Figure("ratio", m.group(0).strip(), f"{_plain(raw)} out of {_plain(den)}", raw / den,
                                    raw, m.start()))
                taken.append((m.start(), m.end()))
                continue
            if kind == "measure":
                unit = UNITS[groups["unit"].lower()]
                said_in_words = m.group("num")[:1].isalpha()
                # Words need a real amount: "one day", "a week", "two years ago" are not statistics,
                # "twelve kilometres" and "forty minutes" are. Digits are read as before.
                if said_in_words and (raw < 2 or (unit in TIME_UNITS and raw < 10)):
                    continue
                if not said_in_words and unit in TIME_UNITS and _is_year(m.group("num"), raw):
                    continue  # "2025 years"? leave it to the year rule
                found.append(Figure("measure", m.group(0).strip(), f"{_plain(raw)} {unit}", raw, raw, m.start()))
                taken.append((m.start(), m.end()))
                continue
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
CARD_RULES_VERSION = "3"
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
