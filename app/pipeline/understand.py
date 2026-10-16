"""Scene understanding: what should the viewer SEE while this line is spoken?

For every scene we produce a SceneMeaning:
- 3 search queries that describe the MEANING of the line (in the context of the whole
  script), e.g. "Cloud computing lets businesses..." -> "data center server racks", not "sky".
- flags: abstract, statistic, named_person, brand, sensitive
- a short reason (stored in timeline.json)

Adapters (picked by UNDERSTAND_PROVIDER), same idea as tts.py:
- GroqUnderstander:  an LLM (LLM_MODEL) answers in JSON. Timeout + LLM_RETRIES retries.
                     If it fails, understand_scenes() uses the rules instead + a warning.
- RulesUnderstander: no network. A small "sense dictionary" picks the meaning of
                     double-meaning words (cloud, bank, mouse, python, apple ...) from the
                     words around them.
- MockUnderstander:  simplest possible queries (tests).

The detectors for numbers, names, brands and sensitive topics are plain code and run for
EVERY adapter, so safety never depends on the LLM getting it right.

Repeatable: Groq is called with temperature LLM_TEMPERATURE (default 0), a fixed seed and a
fixed prompt. Every scene's answer is stored in timeline.json with a fingerprint (hash of the
text, script, style, provider, model and prompt). A re-render (or a clip swap) passes that
timeline back and gets the SAME understanding without asking the LLM again.
"""
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field

import httpx

from app.config import get_settings

log = logging.getLogger("app.pipeline.understand")

FLAG_NAMES = ("abstract", "statistic", "named_person", "brand", "sensitive")

STOPWORDS = set("""a about above after again against all also am an and any are as at be because been before being
below between both but by can could did do does doing down during each few for from further had has have having he
her here hers herself him himself his how i if in into is it its itself just let lets me more most my myself no nor
not now of off on once one only or other our ours ourselves out over own same she should so some such than that the
their theirs them themselves then there these they this those through to too under until up very was we were what
when where which while who whom why will with would you your yours yourself yourselves new way ways get gets got
make makes made many much every thing things time times really something anything anywhere everything""".split())


@dataclass
class SceneMeaning:
    index: int
    queries: list[str]
    flags: dict[str, bool]
    reason: str
    source: str                                       # groq | rules | mock
    names: list[str] = field(default_factory=list)    # people / brands found in the text
    number: str | None = None                          # headline figure for a statistic, e.g. "40%"
    keyword: str = ""                                  # short label for a generated card
    fingerprint: str = ""                              # what this answer depends on (see fingerprint())

    def to_dict(self) -> dict:
        return {"queries": self.queries, "flags": self.flags, "reason": self.reason, "source": self.source,
                "names": self.names, "number": self.number, "keyword": self.keyword,
                "fingerprint": self.fingerprint}

    @classmethod
    def from_dict(cls, index: int, data: dict) -> "SceneMeaning":
        return cls(index=index, queries=list(data["queries"]), flags=dict(data["flags"]), reason=data["reason"],
                   source=data["source"], names=list(data.get("names") or []), number=data.get("number"),
                   keyword=data.get("keyword") or "", fingerprint=data.get("fingerprint") or "")


class UnderstandError(Exception):
    """The LLM call failed or gave an unusable answer."""


# ---------- Small text helpers ----------

def tokens(text: str) -> list[str]:
    """Lower-case words without punctuation: "Cloud computing, now!" -> ["cloud", "computing", "now"]."""
    return re.findall(r"[a-z0-9][a-z0-9'’\-]*", text.lower())


def content_words(text: str) -> list[str]:
    """Words that carry meaning (no stopwords, no numbers), in order, no repeats."""
    seen: dict[str, None] = {}
    for t in tokens(text):
        t = t.strip("'’-")
        if len(t) > 2 and t.isalpha() and t not in STOPWORDS:
            seen.setdefault(t, None)
    return list(seen)


def _singular(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


# ---------- Detector 1: numbers -> statistic ----------

_NUM = r"\d[\d,]*(?:\.\d+)?"
_PERCENT = re.compile(rf"({_NUM})\s*(%|percent\b|per cent\b)", re.I)
_MONEY = re.compile(rf"([$€£₹])\s?({_NUM})(\s*(?:million|billion|thousand|trillion|bn|m|k)\b)?", re.I)
_UNIT = re.compile(rf"\b({_NUM})\s*(million|billion|thousand|trillion|times|x)\b", re.I)
_PLAIN = re.compile(rf"\b({_NUM})\b")
_CHANGE = re.compile(r"\b(doubled|tripled|halved|quadrupled)\b", re.I)


def _is_year(number: str) -> bool:
    return bool(re.fullmatch(r"(19|20)\d\d", number))


def detect_statistic(text: str) -> str | None:
    """The headline figure of a line, or None. A year on its own ("In 2025") is not a statistic."""
    if m := _PERCENT.search(text):
        return f"{m.group(1)}%"
    if m := _MONEY.search(text):
        return f"{m.group(1)}{m.group(2)}{(m.group(3) or '').rstrip()}"
    if m := _UNIT.search(text):
        unit = m.group(2).lower()
        return f"{m.group(1)}x" if unit in ("times", "x") else f"{m.group(1)} {unit}"
    for m in _PLAIN.finditer(text):
        if not _is_year(m.group(1)):
            return m.group(1)
    if m := _CHANGE.search(text):
        return m.group(1).lower()
    return None


# ---------- Detector 2: named people and brands ----------

# "apple" is not here: the sense dictionary decides fruit vs company from the context.
BRANDS = {"google", "microsoft", "amazon", "tesla", "nike", "adidas", "coca-cola", "pepsi", "facebook",
          "instagram", "youtube", "netflix", "samsung", "starbucks", "mcdonald's", "toyota", "disney", "uber",
          "twitter", "tiktok", "openai", "iphone", "ipad", "macbook", "spotify", "whatsapp", "linkedin", "airbnb",
          "ikea", "lego", "bmw", "mercedes", "ferrari", "intel", "nvidia", "sony", "huawei", "xiaomi", "paypal"}
ORG_WORDS = {"inc", "corp", "corporation", "ltd", "llc", "company", "group", "airlines", "motors", "technologies",
             "bank", "foundation", "university", "institute"}
PLACES = {"new york", "united states", "united kingdom", "silicon valley", "hong kong", "los angeles",
          "san francisco", "new delhi", "south africa", "sri lanka", "saudi arabia", "new zealand", "north america",
          "south america", "middle east", "european union", "tamil nadu", "las vegas", "white house"}
NOT_NAMES = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "january", "february",
             "march", "april", "may", "june", "july", "august", "september", "october", "november", "december",
             "english", "internet", "i"}
TITLES = {"mr", "mrs", "ms", "dr", "prof", "president", "minister", "ceo", "sir", "king", "queen", "prince",
          "senator", "judge", "captain", "pope"}


def _clean_word(raw: str) -> str:
    """'Musk's,' -> 'Musk' (no punctuation, no possessive 's)."""
    word = raw.strip(".,;:!?\"'()“”‘’")
    return word[:-2] if word.lower().endswith(("'s", "’s")) else word


def _is_capitalised(word: str) -> bool:
    return bool(re.fullmatch(r"[A-Z][A-Za-z'’\-]*", word)) and word.lower() not in NOT_NAMES


def detect_names(text: str) -> tuple[list[str], list[str]]:
    """Find (people, brands) by spelling.

    - A known brand word (Google, Nike ...) anywhere -> brand.
    - 2+ Capitalised words in a row ("Elon Musk") -> a possible named person, unless it is a
      known place ("New York") or has an organisation word ("World Bank", "Acme Inc") -> brand.
    - A title first ("Dr Jane Smith", "President Lincoln") -> the name after it is a person.
    A single Capitalised word never counts as a person: every sentence starts with one.
    """
    people: list[str] = []
    brands: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        run: list[str] = []  # Capitalised words in a row
        for raw in sentence.split() + [""]:  # the "" at the end closes the last run
            word = _clean_word(raw)
            if word.lower() in BRANDS:
                brands.append(word)
            if _is_capitalised(word):
                run.append(word)
                if not raw.endswith((",", ";", ":")):  # a comma ends a name: "Google, Nike and ..."
                    continue
            _name_from_run(run, people, brands)
            run = []
    return list(dict.fromkeys(people)), list(dict.fromkeys(brands))


def _name_from_run(run: list[str], people: list[str], brands: list[str]) -> None:
    while run and run[0].lower() in STOPWORDS:  # "In", "The" ... are not part of a name
        run = run[1:]
    if len(run) < 2:
        return
    lowered = [w.lower() for w in run]
    if any(w in BRANDS for w in lowered) or " ".join(lowered) in PLACES:
        return  # brand already recorded; places are fine to show
    if lowered[0] in TITLES:
        people.append(" ".join(run[1:]))
    elif any(w in ORG_WORDS for w in lowered):
        brands.append(" ".join(run))
    else:
        people.append(" ".join(run))


# ---------- Detector 3: sensitive topics ----------

SENSITIVE = re.compile(
    r"\b(death|deaths|dead|die|died|dying|kill\w*|cancer|war|wars|suicid\w*|abus\w*|assault\w*|attack\w*|shoot\w*|"
    r"terror\w*|disaster\w*|earthquake\w*|flood\w*|tsunami\w*|hurricane\w*|funeral\w*|grie\w*|mourn\w*|illness\w*|"
    r"disease\w*|pandemic\w*|covid|accident\w*|crash\w*|victim\w*|violen\w*|traged\w*|tragic|poverty|hunger|famine|"
    r"refugee\w*|homeless\w*|depress\w*|addict\w*|overdos\w*|murder\w*|injur\w*|wounded|bomb\w*|hostage\w*)\b",
    re.I)


def detect_sensitive(text: str) -> list[str]:
    return list(dict.fromkeys(m.group(1).lower() for m in SENSITIVE.finditer(text)))


# ---------- Rules: the sense dictionary ----------

@dataclass
class Sense:
    name: str
    context: set[str]
    queries: list[str]
    brand: bool = False


# For each double-meaning word: its senses, the words that point to each sense, and what
# to search for. The FIRST sense is the everyday meaning, used when nothing points elsewhere.
SENSES: dict[str, list[Sense]] = {
    "cloud": [
        Sense("sky", {"sky", "rain", "weather", "storm", "sun", "sunny", "white", "fluffy", "blue", "flying", "plane",
                      "dark", "thunder", "horizon"},
              ["white clouds moving across blue sky", "time lapse of clouds", "sunlight through clouds"]),
        Sense("computing", {"computing", "computer", "computers", "data", "server", "servers", "storage", "software",
                            "online", "internet", "digital", "app", "apps", "business", "businesses", "technology",
                            "tech", "upload", "backup", "network", "remote", "files", "platform"},
              ["data center server racks", "network cables in a server room", "people working on laptops in office"]),
    ],
    "bank": [
        Sense("money", {"money", "account", "accounts", "loan", "loans", "savings", "saving", "deposit", "cash",
                        "interest", "credit", "mortgage", "finance", "financial", "payment", "atm", "branch"},
              ["bank building exterior", "atm cash withdrawal", "banknotes being counted"]),
        Sense("river", {"river", "water", "fishing", "shore", "stream", "grass", "walked", "boat", "sat", "flood",
                        "muddy", "canal", "lake"},
              ["grassy river bank", "calm river flowing past the shore", "fishing on a riverside"]),
    ],
    "value": [
        Sense("worth", {"price", "prices", "cost", "market", "stock", "stocks", "dollar", "money", "invest",
                        "investment", "worth", "property", "sell", "buy", "profit", "savings"},
              ["coins stacked in rising columns", "price tags in a shop", "stock market chart on screen"]),
        Sense("principles", {"values", "honesty", "family", "trust", "culture", "integrity", "respect", "believe",
                             "principles", "kindness", "community", "moral"},
              ["hands holding together", "compass on an old map", "family walking together at sunset"]),
    ],
    "mouse": [
        Sense("animal", {"cat", "cheese", "field", "tiny", "animal", "nest", "grass", "pet", "squeak", "house"},
              ["small mouse in grass", "field mouse close to the ground", "mouse eating seeds"]),
        Sense("computer", {"click", "clicks", "computer", "keyboard", "screen", "desk", "cursor", "laptop", "scroll",
                           "pc", "gaming", "office"},
              ["hand using a computer mouse on desk", "computer mouse and keyboard", "clicking a mouse at a workstation"]),
    ],
    "python": [
        Sense("snake", {"snake", "zoo", "jungle", "reptile", "wild", "scales", "venom", "forest", "bite"},
              ["python snake on a tree branch", "snake moving through grass", "reptile skin close detail"]),
        Sense("programming", {"code", "coding", "programming", "developer", "developers", "script", "scripts",
                              "software", "learn", "language", "data", "program", "programs", "computer", "app"},
              ["programming code on a computer screen", "developer typing code on laptop", "lines of code scrolling"]),
    ],
    "apple": [
        Sense("fruit", {"fruit", "eat", "eating", "orchard", "tree", "juice", "pie", "healthy", "red", "green",
                        "fresh", "sweet", "harvest", "day"},
              ["red apples in an orchard", "fresh apples in a basket", "hand picking an apple from a tree"]),
        Sense("company", {"iphone", "mac", "ipad", "tech", "technology", "company", "shares", "store", "ceo",
                          "launch", "launched", "brand", "device", "devices", "smartphone", "stock"},
              ["smartphone on a wooden table", "modern technology store interior", "person using a laptop"],
              brand=True),
    ],
    "java": [
        Sense("coffee", {"coffee", "cup", "drink", "morning", "cafe", "beans", "brew"},
              ["steaming cup of coffee", "coffee beans roasting", "pouring coffee in a cafe"]),
        Sense("programming", {"code", "coding", "programming", "developer", "software", "language", "app", "program"},
              ["programming code on a computer screen", "developer typing code on laptop", "software team at desks"]),
    ],
    "crane": [
        Sense("bird", {"bird", "birds", "wings", "lake", "wetland", "flying", "fly", "nest", "wildlife"},
              ["crane bird standing in wetland", "cranes flying over a lake", "wading bird in shallow water"]),
        Sense("machine", {"building", "construction", "site", "lift", "lifting", "tower", "steel", "cargo", "port",
                          "workers", "skyscraper"},
              ["construction crane lifting steel", "tower cranes over a building site", "cargo crane at a port"]),
    ],
    "bat": [
        Sense("animal", {"cave", "night", "wings", "flying", "vampire", "dark", "fruit", "animal"},
              ["bat flying at dusk", "bats leaving a cave", "bat hanging upside down"]),
        Sense("sport", {"cricket", "baseball", "ball", "hit", "player", "game", "batting", "pitch", "match"},
              ["cricket bat hitting a ball", "baseball player swinging a bat", "cricket match on a sunny field"]),
    ],
    "spring": [
        Sense("season", {"flowers", "season", "blossom", "garden", "warm", "april", "march", "bloom", "green"},
              ["spring flowers blooming", "cherry blossom branches", "green meadow in spring sunshine"]),
        Sense("coil", {"metal", "coil", "mechanism", "mattress", "steel", "bounce", "engine"},
              ["metal coil spring close detail", "springs in a mechanism", "bouncing metal spring"]),
    ],
    "virus": [
        Sense("illness", {"sick", "flu", "health", "hospital", "doctor", "infection", "vaccine", "disease", "cold"},
              ["microscope in a laboratory", "scientist looking at samples", "hand washing with soap"]),
        Sense("computer", {"computer", "malware", "software", "hacker", "hackers", "antivirus", "files", "email",
                           "security", "laptop", "data"},
              ["warning message on a computer screen", "cyber security code on screen", "laptop with lock icon"]),
    ],
}

# Ordinary words with an obvious picture. abstract=True: an idea, not a thing.
TOPICS: dict[str, tuple[list[str], bool]] = {
    "saving": (["coins dropping into a glass jar", "piggy bank on a table", "hand putting money in a jar"], False),
    "savings": (["coins dropping into a glass jar", "piggy bank on a table", "hand putting money in a jar"], False),
    "money": (["counting banknotes", "coins on a wooden table", "wallet with cash"], False),
    "earning": (["office worker at a desk", "paycheck and calculator", "wallet with banknotes"], False),
    "income": (["paycheck and calculator", "office worker at a desk", "wallet with banknotes"], False),
    "spending": (["paying with a card at a shop counter", "shopping bags in hands", "receipt and calculator on table"],
                 False),
    "shopping": (["shopping bags in hands", "shopping cart in a supermarket", "paying with a card at a shop"], False),
    "budget": (["calculator and notebook budget", "receipt and calculator on table", "planning finances on paper"],
               False),
    "business": (["small shop owner opening store", "cafe owner at counter", "team meeting in a small office"], False),
    "businesses": (["small shop owner opening store", "cafe owner at counter", "team meeting in a small office"],
                   False),
    "office": (["modern office interior", "people working at desks", "laptop on an office desk"], False),
    "remote": (["working on laptop at a cafe", "laptop on a kitchen table", "video call on a laptop"], False),
    "family": (["family walking in a park", "family dinner table", "parents and child at home"], False),
    "growth": (["plant growing time lapse", "rising chart on a screen", "seedling in soil"], False),
    "grew": (["plant growing time lapse", "rising chart on a screen", "seedling in soil"], False),
    "computer": (["laptop on a desk", "typing on a keyboard", "computer screen in an office"], False),
    "technology": (["circuit board close detail", "modern technology devices", "data center server racks"], False),
    "phone": (["smartphone in hand", "scrolling on a phone", "phone on a table"], False),
    "city": (["city skyline at dusk", "busy city street", "aerial view of a city"], False),
    "health": (["fresh vegetables on a table", "person jogging in a park", "doctor's stethoscope on desk"], False),
    "food": (["fresh food on a table", "cooking in a kitchen", "vegetables at a market"], False),
    "education": (["students in a classroom", "books on a library shelf", "writing in a notebook"], False),
    "learning": (["writing in a notebook", "books on a library shelf", "online class on a laptop"], False),
    "travel": (["airplane taking off", "suitcase at an airport", "road trip through hills"], False),
    "nature": (["forest with sunlight", "mountain landscape", "river flowing through forest"], False),
    "ocean": (["ocean waves on a beach", "aerial view of the sea", "sunset over the ocean"], False),
    "climate": (["wind turbines on a hill", "solar panels in sunlight", "melting ice"], False),
    "energy": (["solar panels in sunlight", "wind turbines turning", "power lines at sunset"], False),
    # Sensitive topics: calm, respectful pictures only (safety.py also adds "calm" and blocks graphic words).
    "war": (["candle light in the dark", "white flowers at a memorial", "empty quiet street at dawn"], False),
    "death": (["white flowers at a memorial", "candle light in the dark", "rain on a window"], False),
    "dead": (["white flowers at a memorial", "candle light in the dark", "rain on a window"], False),
    "died": (["white flowers at a memorial", "candle light in the dark", "rain on a window"], False),
    "funeral": (["white flowers at a memorial", "candle light in the dark", "quiet cemetery with trees"], False),
    "grief": (["rain on a window", "candle light in the dark", "hands holding in support"], False),
    "cancer": (["hospital corridor in soft light", "hands holding in support", "doctor holding a stethoscope"], False),
    "illness": (["hospital corridor in soft light", "hands holding in support", "doctor holding a stethoscope"],
                False),
    "disease": (["microscope in a laboratory", "hospital corridor in soft light", "hands holding in support"], False),
    "hospital": (["hospital corridor in soft light", "doctor holding a stethoscope", "hands holding in support"],
                 False),
    "disaster": (["rescue workers helping", "calm sea after a storm", "volunteers handing out supplies"], False),
    "earthquake": (["rescue workers helping", "volunteers handing out supplies", "cracked ground"], False),
    "flood": (["rain on a window", "volunteers handing out supplies", "calm river after rain"], False),
    "accident": (["ambulance lights at night", "hospital corridor in soft light", "rain on a window"], False),
    "poverty": (["hands holding in support", "volunteers handing out food", "empty bowl on a table"], False),
    "time": (["clock ticking", "hourglass with sand", "calendar pages"], True),
    "trust": (["two people shaking hands", "hands holding together", "handshake in an office"], True),
    "honest": (["handshake in an office", "open hands", "signing a document"], True),
    "honesty": (["handshake in an office", "open hands", "signing a document"], True),
    "decision": (["signpost with two directions", "path splitting in a forest", "person thinking at a window"], True),
    "wisely": (["person thinking at a window", "chess pieces on a board", "planning in a notebook"], True),
    "success": (["mountain summit at sunrise", "team celebrating in office", "trophy on a table"], True),
    "future": (["sunrise over a city", "road leading to the horizon", "child looking at the sky"], True),
    "hope": (["sunrise through clouds", "candle light in the dark", "seedling in soil"], True),
    "freedom": (["bird flying in the sky", "open road to the horizon", "person on a hilltop"], True),
    "love": (["holding hands at sunset", "heart shape in hands", "family hug at home"], True),
    "idea": (["light bulb glowing", "writing ideas on sticky notes", "brainstorm on a whiteboard"], True),
    "ideas": (["light bulb glowing", "writing ideas on sticky notes", "brainstorm on a whiteboard"], True),
    "wisdom": (["old books on a shelf", "chess pieces on a board", "elderly hands on a book"], True),
    "patience": (["hourglass with sand", "slow river flowing", "tree growing time lapse"], True),
    "slowly": (["hourglass with sand", "slow river flowing", "snail moving slowly"], True),
    "happiness": (["sunflower field in sunlight", "friends laughing outdoors", "child running in a field"], True),
    "change": (["autumn leaves changing color", "caterpillar to butterfly", "road with a turning point"], True),
    "risk": (["tightrope walker", "stormy sea", "dice rolling on a table"], True),
    "goal": (["arrow hitting a target", "mountain summit at sunrise", "runner crossing finish line"], True),
    "goals": (["arrow hitting a target", "mountain summit at sunrise", "runner crossing finish line"], True),
}
ABSTRACT_WORDS = {w for w, (_, abstract) in TOPICS.items() if abstract} | {
    "meaning", "values", "integrity", "kindness", "courage", "faith", "truth", "justice", "respect", "dream",
    "dreams", "purpose", "opportunity", "potential", "balance", "mindset", "motivation", "confidence", "earned"}


def _context_score(sense: Sense, scene_words: list[str], script_words: list[str]) -> int:
    """Words near the double-meaning word count double; the rest of the script counts once."""
    return (2 * sum(_singular(w) in sense.context or w in sense.context for w in scene_words)
            + sum(_singular(w) in sense.context or w in sense.context for w in script_words))


def keyword_queries(words: list[str]) -> list[str]:
    """["launched", "rocket", "texas"] -> ["launched rocket texas", "rocket texas", "launched rocket"]."""
    if not words:
        return []
    return [" ".join(words[:3]), " ".join(words[1:3]), " ".join(words[:2]), words[0]]


def card_keyword(text: str, number: str | None, names: list[str] | tuple = ()) -> str:
    """Short label for a generated motion card (never a person's or brand's name)."""
    name_words = {w.lower() for n in names for w in n.split()}
    words = [w for w in content_words(text) if w not in name_words]
    if number:
        return " ".join(w for w in words if not w.isdigit() and w not in ("percent", "cent"))[:40].strip() or "the numbers"
    for w in words:
        if w in ABSTRACT_WORDS:
            return w
    return " ".join(words[:2]) or "keep watching"


def safety_facts(text: str) -> dict:
    """Run the code detectors (all adapters use these)."""
    people, brands = detect_names(text)
    number = detect_statistic(text)
    return {"people": people, "brands": brands, "number": number, "sensitive": detect_sensitive(text)}


class Understander:
    name = "base"

    def understand(self, scenes: list, full_text: str, style: str | None = None) -> list[SceneMeaning]:
        raise NotImplementedError

    def info(self) -> dict:
        return {"provider": self.name}


class RulesUnderstander(Understander):
    name = "rules"

    def understand_one(self, index: int, text: str, full_text: str, visual_hint: str | None = None) -> SceneMeaning:
        scene_words = tokens(text)
        script_words = tokens(full_text)
        facts = safety_facts(text)
        queries: list[str] = []
        reasons: list[str] = []
        brand_sense = False
        concrete = 0
        abstract_hits = []

        if visual_hint and visual_hint.strip():
            queries.append(visual_hint.strip()[:80])
            reasons.append("visual_hint used as the first query")

        for word in scene_words:
            key = _singular(word) if _singular(word) in SENSES else word
            if key in SENSES:
                senses = SENSES[key]
                best = max(senses, key=lambda sn: _context_score(sn, scene_words, script_words))  # ties -> first
                queries += best.queries
                brand_sense = brand_sense or best.brand
                concrete += 1
                reasons.append(f"'{key}' read as {best.name}")
        for word in content_words(text):
            if word in TOPICS:
                topic_queries, is_abstract = TOPICS[word]
                queries += topic_queries
                if is_abstract:
                    abstract_hits.append(word)
                else:
                    concrete += 1
            elif word in ABSTRACT_WORDS:
                abstract_hits.append(word)
        if facts["number"] and concrete == 0:
            reasons.append(f"statistic {facts['number']}")

        words = [w for w in content_words(text) if w not in {b.lower() for b in facts["brands"]}
                 and not any(w in p.lower().split() for p in facts["people"])]
        queries += keyword_queries(words)  # plain keyword queries as a last resort
        if not queries:
            queries.append("abstract background light")

        brands = facts["brands"] + (["Apple"] if brand_sense and "Apple" not in facts["brands"] else [])
        flags = {
            "abstract": bool(abstract_hits) and concrete == 0,
            "statistic": facts["number"] is not None,
            "named_person": bool(facts["people"]),
            "brand": bool(brands) or brand_sense,
            "sensitive": bool(facts["sensitive"]),
        }
        if flags["abstract"]:
            reasons.append(f"abstract idea ({', '.join(abstract_hits[:3])})")
        if not reasons:
            reasons.append("keywords of the line")
        return SceneMeaning(index=index, queries=_three(queries), flags=flags, reason="rules: " + "; ".join(reasons),
                            source=self.name, names=facts["people"] + brands, number=facts["number"],
                            keyword=card_keyword(text, facts["number"], facts["people"] + brands))

    def understand(self, scenes, full_text, style=None):
        return [self.understand_one(sc.index, sc.text, full_text, sc.visual_hint) for sc in scenes]


def _three(queries: list[str]) -> list[str]:
    """First 3 different queries, padded by repeating the last one if needed."""
    unique = list(dict.fromkeys(q.strip() for q in queries if q and q.strip()))[:3]
    while len(unique) < 3:
        unique.append(unique[-1] if unique else "abstract background light")
    return unique


class MockUnderstander(Understander):
    """Queries are just the line's keywords. Flags come from the shared detectors."""
    name = "mock"

    def understand(self, scenes, full_text, style=None):
        out = []
        for sc in scenes:
            words = content_words(sc.text) or ["background"]
            facts = safety_facts(sc.text)
            queries = ([sc.visual_hint] if sc.visual_hint else []) + keyword_queries(words)
            out.append(SceneMeaning(
                index=sc.index, queries=_three(queries), source=self.name, reason="mock: keywords of the line",
                flags={"abstract": False, "statistic": facts["number"] is not None,
                       "named_person": bool(facts["people"]), "brand": bool(facts["brands"]),
                       "sensitive": bool(facts["sensitive"])},
                names=facts["people"] + facts["brands"], number=facts["number"],
                keyword=card_keyword(sc.text, facts["number"], facts["people"] + facts["brands"])))
        return out


GROQ_SYSTEM_PROMPT = """You choose B-roll (background stock footage) for a narrated video.
For EACH scene write exactly 3 short stock-footage search queries (2-6 English words) that show what
the line MEANS in the context of the whole script - not its literal words. Example: "Cloud computing
helps small firms" -> "data center server racks", never "sky". Use the visual_hint if one is given.
Rules: never put a real person's name or a brand name in a query - describe generic related footage
instead, without faces. For sad or sensitive topics choose calm, respectful imagery, nothing graphic.
Respect the avoid list. Flags (true/false): abstract = an idea with no obvious physical subject;
statistic = contains a number, percentage or amount; named_person = mentions a specific real person;
brand = mentions a company or product brand; sensitive = death, illness, violence, disaster, abuse,
tragedy or similar. reason = one short sentence on why these queries fit the meaning.
Prefer the most common, concrete stock-footage wording; list the scenes in index order.
Answer with JSON only, in this shape:
{"scenes": [{"index": 0, "queries": ["...", "...", "..."], "flags": {"abstract": false, "statistic": false,
"named_person": false, "brand": false, "sensitive": false}, "reason": "..."}]}"""
# Changes whenever the prompt text changes, so an answer stored in timeline.json is only reused
# for the exact same prompt.
PROMPT_VERSION = hashlib.sha1(GROQ_SYSTEM_PROMPT.encode()).hexdigest()[:10]


class GroqUnderstander(Understander):
    name = "groq"

    def __init__(self):
        self.tokens_used = 0

    def info(self):
        s = get_settings()
        return {"provider": self.name, "model": s.LLM_MODEL, "endpoint": s.GROQ_BASE_URL,
                "temperature": s.LLM_TEMPERATURE, "seed": s.SEED, "prompt_version": PROMPT_VERSION,
                "tokens_used": self.tokens_used}

    def _call(self, payload: dict) -> dict:
        s = get_settings()
        resp = httpx.post(f"{s.GROQ_BASE_URL.rstrip('/')}/chat/completions", json=payload,
                          headers={"Authorization": f"Bearer {s.GROQ_API_KEY}"}, timeout=s.LLM_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            raise UnderstandError(f"HTTP {resp.status_code}")
        body = resp.json()
        self.tokens_used += int((body.get("usage") or {}).get("total_tokens") or 0)
        try:
            return json.loads(body["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
            raise UnderstandError("the answer was not valid JSON") from e

    def understand(self, scenes, full_text, style=None):
        s = get_settings()
        if not s.GROQ_API_KEY:
            raise UnderstandError("GROQ_API_KEY is empty")
        user = {"full_script": full_text, "style": style,
                "scenes": [{"index": sc.index, "text": sc.text, "visual_hint": sc.visual_hint} for sc in scenes]}
        # Deterministic request: fixed prompt, keys always in the same order, temperature 0
        # (LLM_TEMPERATURE) and a fixed seed -> the same script gives the same request every time.
        payload = {"model": s.LLM_MODEL, "temperature": s.LLM_TEMPERATURE, "seed": s.SEED,
                   "response_format": {"type": "json_object"},
                   "messages": [{"role": "system", "content": GROQ_SYSTEM_PROMPT},
                                {"role": "user", "content": json.dumps(user, ensure_ascii=False, sort_keys=True)}]}
        last_error: Exception | None = None
        for attempt in range(1 + max(0, s.LLM_RETRIES)):
            try:
                answer = self._call(payload)
                return self._parse(answer, scenes)
            except (UnderstandError, httpx.HTTPError, ValueError) as e:
                last_error = e
                log.warning("groq attempt %s failed: %s", attempt + 1, e)
                if attempt < s.LLM_RETRIES:
                    time.sleep(1.0)
        raise UnderstandError(str(last_error) if not isinstance(last_error, httpx.HTTPError)
                              else type(last_error).__name__)

    def _parse(self, answer: dict, scenes) -> list[SceneMeaning]:
        items = {int(it.get("index", -1)): it for it in (answer.get("scenes") or []) if isinstance(it, dict)}
        if not items:
            raise UnderstandError("the answer has no scenes")
        out = []
        for sc in scenes:
            item = items.get(sc.index)
            if item is None:
                raise UnderstandError(f"the answer misses scene {sc.index}")
            queries = [str(q).strip()[:80] for q in (item.get("queries") or []) if str(q).strip()]
            if not queries:
                raise UnderstandError(f"scene {sc.index} has no queries")
            raw_flags = item.get("flags") or {}
            facts = safety_facts(sc.text)
            # Safety flags: the LLM's answer OR the code detectors (never weaker than the rules).
            flags = {name: bool(raw_flags.get(name)) for name in FLAG_NAMES}
            flags["statistic"] |= facts["number"] is not None
            flags["named_person"] |= bool(facts["people"])
            flags["brand"] |= bool(facts["brands"])
            flags["sensitive"] |= bool(facts["sensitive"])
            out.append(SceneMeaning(index=sc.index, queries=_three(queries), flags=flags, source=self.name,
                                    reason="groq: " + str(item.get("reason") or "").strip()[:200],
                                    names=facts["people"] + facts["brands"], number=facts["number"],
                                    keyword=card_keyword(sc.text, facts["number"], facts["people"] + facts["brands"])))
        return out


ADAPTERS = {"groq": GroqUnderstander, "rules": RulesUnderstander, "mock": MockUnderstander}


def fingerprint(provider: str, scene, full_text: str, style: str | None) -> str:
    """A short hash of everything an answer depends on. Same fingerprint = the same question,
    so a stored answer can be reused instead of asking again."""
    s = get_settings()
    data = {"provider": provider, "text": scene.text, "visual_hint": scene.visual_hint, "script": full_text,
            "style": style}
    if provider == "groq":
        data.update(model=s.LLM_MODEL, prompt=PROMPT_VERSION, temperature=s.LLM_TEMPERATURE, seed=s.SEED)
    return hashlib.sha1(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def reuse_from_timeline(previous: dict | None, scenes: list, full_text: str,
                        style: str | None) -> list[SceneMeaning] | None:
    """The meanings stored in an earlier timeline.json, if EVERY scene's fingerprint still matches
    (same text, script, style, provider, model, prompt). Otherwise None = ask again."""
    old = (previous or {}).get("scenes") or []
    if len(old) != len(scenes):
        return None
    provider = get_settings().UNDERSTAND_PROVIDER
    out = []
    for sc, item in zip(scenes, old):
        stored = item.get("understanding") or {}
        if not stored.get("queries") or stored.get("fingerprint") != fingerprint(provider, sc, full_text, style):
            return None
        out.append(SceneMeaning.from_dict(sc.index, stored))
    return out


def understand_scenes(scenes: list, full_text: str, style: str | None = None,
                      previous_timeline: dict | None = None) -> tuple[list[SceneMeaning], dict, list[str]]:
    """Returns (one meaning per scene, info for the manifest, warnings). Never raises for LLM trouble.

    previous_timeline: an earlier timeline.json of the same job (re-render / clip swap). If its stored
    understanding still matches, it is reused and the LLM is NOT asked again.
    """
    provider = get_settings().UNDERSTAND_PROVIDER
    reused = reuse_from_timeline(previous_timeline, scenes, full_text, style)
    if reused is not None:
        return reused, {"provider": provider, "reused_from_timeline": True, "tokens_used": 0}, []
    adapter = ADAPTERS[provider]()
    try:
        meanings, info, warnings = adapter.understand(scenes, full_text, style), adapter.info(), []
    except UnderstandError as e:
        if provider != "groq":
            raise
        rules = RulesUnderstander()
        warning = f"Scene understanding with Groq failed ({e}); used the offline rules instead"
        meanings, info, warnings = (rules.understand(scenes, full_text, style), {**adapter.info(), "fallback": "rules"},
                                    [warning])
    for meaning, sc in zip(meanings, scenes):
        # Uses the provider that REALLY answered: a rules fallback is never reused as a Groq answer.
        meaning.fingerprint = fingerprint(meaning.source, sc, full_text, style)
    return meanings, info, warnings
