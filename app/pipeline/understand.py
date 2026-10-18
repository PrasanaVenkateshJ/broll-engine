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

Non-English narration (e.g. Tamil, Hindi): a scene may carry `search_text`, Whisper's English
translation of it. Queries are always English: the rules and mock adapters read the English, Groq gets
both the original line and the English. Captions and card numbers always come from the spoken words.

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
from app.pipeline import cards

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
    # The generated card's text, built by code from the narration ONLY (cards.build_card).
    card: dict = field(default_factory=dict)
    # What each query is for (Groq: meaning, metaphor, setting, action, mood), same order as queries.
    query_roles: list[str] = field(default_factory=list)
    # The WHOLE story is sad / tragic (death, disaster, illness ...): calm, respectful rules for this scene
    # too, even if the line alone looks neutral. Set by apply_story_sensitivity().
    story_sensitive: bool = False

    def to_dict(self) -> dict:
        return {"queries": self.queries, "query_roles": self.query_roles, "flags": self.flags, "reason": self.reason,
                "source": self.source, "names": self.names, "number": self.number, "keyword": self.keyword,
                "card": self.card, "story_sensitive": self.story_sensitive, "fingerprint": self.fingerprint}

    @classmethod
    def from_dict(cls, index: int, data: dict) -> "SceneMeaning":
        return cls(index=index, queries=list(data["queries"]), flags=dict(data["flags"]), reason=data["reason"],
                   source=data["source"], names=list(data.get("names") or []), number=data.get("number"),
                   keyword=data.get("keyword") or "", fingerprint=data.get("fingerprint") or "",
                   card=dict(data.get("card") or {}), query_roles=list(data.get("query_roles") or []),
                   story_sensitive=bool(data.get("story_sensitive")))


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

def detect_statistic(text: str) -> str | None:
    """The headline figure of a line ("40%", "$1.2M", "3x", "300", "doubled"), or None.
    Read by cards.find_figures (plain code). A year on its own ("In 2025") is not a statistic."""
    figure = cards.main_figure([f for f in cards.find_figures(text) if f.kind != "year"])
    if figure:
        return figure.display
    change = cards.change_word(text)
    return change.text.lower() if change else None


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
    r"refugee\w*|homeless\w*|depress\w*|addict\w*|overdos\w*|murder\w*|injur\w*|wounded|bomb\w*|hostage\w*|"
    # loss of life said gently: "lost their lives", "lost someone", "passed away", "loss of life"
    r"lost (?:their|his|her|our|my|your) lives?|lost (?:someone|a loved one|loved ones)|passed away|loss of life)\b",
    re.I)


def detect_sensitive(text: str) -> list[str]:
    return list(dict.fromkeys(m.group(1).lower() for m in SENSITIVE.finditer(text)))


def story_is_sensitive(meanings: list["SceneMeaning"], llm_says: bool = False) -> tuple[bool, str]:
    """Is the WHOLE story sad / tragic? (yes/no, why). Measurable rule: at least STORY_SENSITIVE_MIN_SCENES
    sensitive scenes, or at least STORY_SENSITIVE_MIN_SHARE of all scenes, or the LLM says so."""
    s = get_settings()
    count = sum(1 for m in meanings if m.flags.get("sensitive"))
    share = count / len(meanings) if meanings else 0.0
    if count and count >= s.STORY_SENSITIVE_MIN_SCENES:
        return True, f"{count} of {len(meanings)} scenes are sensitive (>= {s.STORY_SENSITIVE_MIN_SCENES})"
    if count and share >= s.STORY_SENSITIVE_MIN_SHARE:
        return True, f"{count} of {len(meanings)} scenes are sensitive ({share:.0%} >= {s.STORY_SENSITIVE_MIN_SHARE:.0%})"
    if llm_says:
        return True, "the LLM rated the whole script as sensitive"
    return False, ""


def apply_story_sensitivity(meanings: list["SceneMeaning"], llm_says: bool = False) -> str:
    """Mark every scene of a sensitive story (safety.py then gives ALL of them calm, respectful queries).
    Returns the reason ("" = the story is not sensitive)."""
    sensitive, why = story_is_sensitive(meanings, llm_says)
    for m in meanings:
        m.story_sensitive = sensitive
        if sensitive and not m.flags.get("sensitive"):
            m.reason += f" (story is sensitive: {why} -> calm, respectful footage for this scene too)"
    return why


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
# Abstract ideas -> CONCRETE, filmable visual metaphors (things a camera can actually see). Used by the rules
# adapter before the TOPICS queries; Groq is asked for the same kind of metaphors in its prompt.
METAPHORS: dict[str, list[str]] = {
    "trust": ["two people shaking hands", "hand passing house keys", "friends helping each other climb",
              "child holding a parent's hand", "team catching a falling colleague"],
    "hope": ["sunrise over hills", "green sprout breaking through soil", "light through a window in a dark room",
             "child looking at the sky", "candle glowing in the dark"],
    "patience": ["hourglass with falling sand", "fisherman waiting by a lake", "seedling growing time lapse",
                 "slow river flowing", "gardener watering young plants"],
    "strength": ["tree standing in strong wind", "rock in crashing waves", "athlete lifting weights",
                 "mountain peak above clouds", "hands gripping a rope"],
    "success": ["climber reaching a mountain summit", "runner crossing a finish line", "team celebrating in office",
                "trophy on a table", "arrow hitting a target"],
    "time": ["clock ticking", "hourglass with sand", "calendar pages turning", "city time lapse day to night",
             "old pocket watch close up"],
    "future": ["sunrise over a city", "road leading to the horizon", "child looking through a telescope",
               "seedling in an open hand", "modern city skyline at dawn"],
    "freedom": ["bird flying in the open sky", "open road to the horizon", "person on a hilltop with open arms",
                "kite flying high", "horses running in a field"],
    "love": ["holding hands at sunset", "family hug at home", "elderly couple walking together",
             "mother holding a baby's hand", "heart shape in hands"],
    "wisdom": ["old books on a shelf", "elderly hands on a book", "chess pieces on a board", "owl on a branch",
               "teacher with students"],
    "change": ["autumn leaves changing color", "butterfly leaving a cocoon", "road with a turning point",
               "seasons time lapse of a tree", "caterpillar on a leaf"],
    "risk": ["tightrope walker", "dice rolling on a table", "climber on a steep cliff", "stormy sea waves",
             "person standing at a cliff edge"],
    "courage": ["person standing at a cliff edge", "firefighter walking into smoke", "climber on a steep rock",
                "child jumping into a pool", "surfer riding a big wave"],
    "growth": ["plant growing time lapse", "seedling in soil", "child measuring height on a wall",
               "tree from sapling to tall", "rising stairs"],
    "teamwork": ["team rowing a boat together", "hands stacked together", "people building together",
                 "ants carrying food together", "relay race baton pass"],
    "balance": ["stacked stones by the sea", "yoga pose on a beach", "tightrope walker", "scales balancing",
                "person walking on a log"],
    "honesty": ["handshake in an office", "open hands", "signing a document", "clear water in a stream",
                "person looking into a mirror"],
    "kindness": ["hands giving food to a neighbour", "helping an elderly person cross the street",
                 "volunteers handing out supplies", "child sharing a toy", "hand holding a small bird"],
    "goal": ["arrow hitting a target", "mountain summit at sunrise", "runner crossing finish line",
             "darts hitting a bullseye", "writing goals in a notebook"],
    "dream": ["child looking at the stars", "night sky full of stars", "paper plane flying", "person gazing out a window",
              "hot air balloons at sunrise"],
    "opportunity": ["door opening to bright light", "path through a forest", "keys in a hand",
                    "sunrise over a road", "open window with a view"],
}
METAPHORS["dreams"], METAPHORS["goals"], METAPHORS["honest"] = METAPHORS["dream"], METAPHORS["goal"], METAPHORS["honesty"]
METAPHORS["earned"], METAPHORS["slowly"] = METAPHORS["trust"][:2] + METAPHORS["patience"][:2], METAPHORS["patience"]

ABSTRACT_WORDS = {w for w, (_, abstract) in TOPICS.items() if abstract} | set(METAPHORS) | {
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


def scene_card(text: str, number: str | None, names: list[str] | tuple = ()) -> dict:
    """The generated card for a line, from its narration only (see cards.py)."""
    return cards.build_card(text, card_keyword(text, number, names), names).to_dict()


def safety_facts(text: str, english: str | None = None) -> dict:
    """Run the code detectors (all adapters use these).
    english = the scene's English translation (non-English narration): names, brands and sensitive words
    are looked for in it too. The number only ever comes from the spoken text (a card shows only what the
    narration says; a machine translation can change numbers)."""
    people, brands = detect_names(text)
    sensitive = detect_sensitive(text)
    if english:
        more_people, more_brands = detect_names(english)
        people = list(dict.fromkeys(people + more_people))
        brands = list(dict.fromkeys(brands + more_brands))
        sensitive = list(dict.fromkeys(sensitive + detect_sensitive(english)))
    return {"people": people, "brands": brands, "number": detect_statistic(text), "sensitive": sensitive}


def english_of(scene) -> str | None:
    """The scene's English translation for footage search, or None (English narration / no translation)."""
    return getattr(scene, "search_text", None) or None


def english_script(scenes: list, full_text: str) -> str:
    """The whole script in English for context: the translations if the scenes have them."""
    if any(english_of(sc) for sc in scenes):
        return " ".join(english_of(sc) or sc.text for sc in scenes)
    return full_text


class Understander:
    name = "base"

    def understand(self, scenes: list, full_text: str, style: str | None = None) -> list[SceneMeaning]:
        raise NotImplementedError

    def info(self) -> dict:
        return {"provider": self.name}


class RulesUnderstander(Understander):
    name = "rules"

    def understand_one(self, index: int, text: str, full_text: str, visual_hint: str | None = None,
                       english: str | None = None) -> SceneMeaning:
        """text = the line as spoken (card + number); english = its translation, if any. The queries are
        built from the English (full_text should then be the English script too)."""
        spoken = text
        text = english or spoken  # what the queries are made from
        scene_words = tokens(text)
        script_words = tokens(full_text)
        facts = safety_facts(spoken, english)
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
        metaphors: list[str] = []
        for word in content_words(text):
            if word in TOPICS:
                topic_queries, is_abstract = TOPICS[word]
                if is_abstract:
                    abstract_hits.append(word)
                    metaphors += METAPHORS.get(word, topic_queries)
                else:
                    queries += topic_queries
                    concrete += 1
            elif word in ABSTRACT_WORDS:
                abstract_hits.append(word)
                metaphors += METAPHORS.get(word, [])
        if concrete == 0 and metaphors:  # an abstract line: concrete metaphors are the best pictures it has
            queries = queries[:1] + metaphors + queries[1:] if visual_hint else metaphors + queries
            reasons.append("concrete metaphors for " + ", ".join(dict.fromkeys(abstract_hits)))
        else:
            queries += metaphors
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
        if english:
            reasons.append("queries from the English translation")
        return SceneMeaning(index=index, queries=pick_queries(queries), flags=flags, reason="rules: " + "; ".join(reasons),
                            source=self.name, names=facts["people"] + brands, number=facts["number"],
                            keyword=card_keyword(spoken, facts["number"], facts["people"] + brands),
                            card=scene_card(spoken, facts["number"], facts["people"] + brands))

    def understand(self, scenes, full_text, style=None):
        english_full = english_script(scenes, full_text)
        return [self.understand_one(sc.index, sc.text, english_full, sc.visual_hint, english_of(sc)) for sc in scenes]


def pick_queries(queries: list[str]) -> list[str]:
    """The first QUERIES_PER_SCENE different queries (never padded with repeats; at least one)."""
    unique = list(dict.fromkeys(q.strip() for q in queries if q and q.strip()))
    return unique[:max(1, get_settings().QUERIES_PER_SCENE)] or ["abstract background light"]


class MockUnderstander(Understander):
    """Queries are just the line's keywords. Flags come from the shared detectors."""
    name = "mock"

    def understand(self, scenes, full_text, style=None):
        out = []
        for sc in scenes:
            words = content_words(english_of(sc) or sc.text) or ["background"]
            facts = safety_facts(sc.text, english_of(sc))
            queries = ([sc.visual_hint] if sc.visual_hint else []) + keyword_queries(words)
            out.append(SceneMeaning(
                index=sc.index, queries=pick_queries(queries), source=self.name, reason="mock: keywords of the line",
                flags={"abstract": False, "statistic": facts["number"] is not None,
                       "named_person": bool(facts["people"]), "brand": bool(facts["brands"]),
                       "sensitive": bool(facts["sensitive"])},
                names=facts["people"] + facts["brands"], number=facts["number"],
                keyword=card_keyword(sc.text, facts["number"], facts["people"] + facts["brands"]),
                card=scene_card(sc.text, facts["number"], facts["people"] + facts["brands"])))
        return out


QUERY_ROLES = ("meaning", "metaphor", "setting", "action", "mood")


def groq_system_prompt(n: int) -> str:
    roles = ", ".join(f"{i + 1} {r}" for i, r in enumerate(QUERY_ROLES[:n]))
    return f"""You choose B-roll (background stock footage) for a narrated video.
For EACH scene write exactly {n} short stock-footage search queries (2-6 English words) that show what
the line MEANS in the context of the whole script - not its literal words. Example: "Cloud computing
helps small firms" -> "data center server racks", never "sky". Give them in this order: {roles}
(meaning = what the line means; metaphor = a concrete visual metaphor; setting = a fitting place;
action = people or things doing something that fits; mood = a visual that carries the feeling).
Every query must be something a camera can film. Abstract lines (an idea such as trust, hope,
patience) get CONCRETE filmable metaphors, never the abstract word alone: "Trust is earned slowly" ->
"two people shaking hands", "hand passing house keys", "friends helping each other climb".
Statistic lines get footage of the TOPIC of the number, not charts: "Household savings grew by 40%" ->
"coins dropping into a piggy bank", "family planning a budget at a kitchen table".
Use the visual_hint if one is given.
Rules: never put a real person's name or a brand name in a query - describe generic related footage
instead, without faces. For sad or sensitive topics choose calm, respectful imagery, nothing graphic.
Respect the avoid list. A scene may also have "english": a machine translation of a non-English line
(it can contain errors - follow the meaning of the original line). Queries are ALWAYS in English, even
when the narration is not. Flags (true/false): abstract = an idea with no obvious physical subject;
statistic = contains a number, percentage or amount; named_person = mentions a specific real person;
brand = mentions a company or product brand; sensitive = death, illness, violence, disaster, abuse,
tragedy or similar. reason = one short sentence on why these queries fit the meaning.
Optional "card" (only for a statistic line): {{"headline": the figure exactly as said in the line,
"subtitle": 2-5 words copied from the same sentence}}. Never write a number the line does not say.
"story_sensitive" = true if the WHOLE script is sad or tragic news (death, disaster, illness, violence),
so even its neutral lines need calm, respectful footage.
Prefer the most common, concrete stock-footage wording; list the scenes in index order.
Answer with JSON only, in this shape:
{{"story_sensitive": false, "scenes": [{{"index": 0, "queries": ["...", "..."], "flags": {{"abstract": false,
"statistic": false, "named_person": false, "brand": false, "sensitive": false}}, "reason": "..."}}]}}"""


GROQ_SYSTEM_PROMPT = groq_system_prompt(5)


def prompt_version() -> str:
    """Changes whenever the prompt REALLY sent changes (its text depends on QUERIES_PER_SCENE), so an answer
    stored in timeline.json is only reused for the exact same prompt."""
    return hashlib.sha1(groq_system_prompt(get_settings().QUERIES_PER_SCENE).encode()).hexdigest()[:10]


class RateLimited(UnderstandError):
    """HTTP 429 from the LLM API; retry_after = the server's wait in seconds (None if it did not say)."""

    def __init__(self, retry_after: float | None):
        super().__init__("HTTP 429")
        self.retry_after = retry_after


def retry_after_seconds(headers) -> float | None:
    """The retry-after header in seconds (Groq sends seconds), or None."""
    try:
        return max(0.0, float(headers.get("retry-after")))
    except (TypeError, ValueError):
        return None


class GroqUnderstander(Understander):
    """Asks the LLM in batches of LLM_BATCH_SIZE scenes (each batch also gets the whole script for context).

    Robust to the free tier's limits:
    - HTTP 429: waits the server's retry-after (each wait at most LLM_MAX_RETRY_AFTER_SECONDS, all waits of
      the job together at most LLM_MAX_WAIT_SECONDS) and asks again; this does not use up LLM_RETRIES;
    - other errors / bad JSON: LLM_RETRIES more tries per batch;
    - an answer that misses some scenes: only the missing ones are asked again (once);
    - whatever is still missing gets the offline rules PER SCENE (understand_scenes), not for the whole job.
    """
    name = "groq"

    def __init__(self):
        self.tokens_used = 0
        self.warnings: list[str] = []  # e.g. a card suggestion with a number the narration does not say
        self.calls = 0
        self.rate_limited = 0
        self.waited = 0.0
        self.story_sensitive = False
        self.missing: dict[int, str] = {}  # scene index -> why the LLM gave no usable answer

    def info(self):
        s = get_settings()
        info = {"provider": self.name, "model": s.LLM_MODEL, "endpoint": s.GROQ_BASE_URL,
                "temperature": s.LLM_TEMPERATURE, "seed": s.SEED, "prompt_version": prompt_version(),
                "tokens_used": self.tokens_used, "batch_size": max(1, s.LLM_BATCH_SIZE), "calls": self.calls,
                "rate_limited_answers": self.rate_limited, "seconds_waited_for_rate_limit": round(self.waited, 1)}
        if s.LLM_REASONING_EFFORT:
            info["reasoning_effort"] = s.LLM_REASONING_EFFORT
        return info

    def _call(self, payload: dict) -> dict:
        s = get_settings()
        self.calls += 1
        resp = httpx.post(f"{s.GROQ_BASE_URL.rstrip('/')}/chat/completions", json=payload,
                          headers={"Authorization": f"Bearer {s.GROQ_API_KEY}"}, timeout=s.LLM_TIMEOUT_SECONDS)
        if resp.status_code == 429:
            self.rate_limited += 1
            raise RateLimited(retry_after_seconds(resp.headers))
        if resp.status_code != 200:
            raise UnderstandError(f"HTTP {resp.status_code}")
        body = resp.json()
        self.tokens_used += int((body.get("usage") or {}).get("total_tokens") or 0)
        try:
            return json.loads(body["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
            raise UnderstandError("the answer was not valid JSON") from e

    def _payload(self, scenes, full_text, style) -> dict:
        s = get_settings()
        user = {"full_script": full_text, "style": style,
                "scenes": [{"index": sc.index, "text": sc.text, "visual_hint": sc.visual_hint,
                            **({"english": english_of(sc)} if english_of(sc) else {})} for sc in scenes]}
        # Deterministic request: fixed prompt, keys always in the same order, temperature 0
        # (LLM_TEMPERATURE) and a fixed seed -> the same script gives the same request every time.
        payload = {"model": s.LLM_MODEL, "temperature": s.LLM_TEMPERATURE, "seed": s.SEED,
                   "response_format": {"type": "json_object"},
                   "messages": [{"role": "system", "content": groq_system_prompt(s.QUERIES_PER_SCENE)},
                                {"role": "user", "content": json.dumps(user, ensure_ascii=False, sort_keys=True)}]}
        if s.LLM_REASONING_EFFORT:
            payload["reasoning_effort"] = s.LLM_REASONING_EFFORT
        return payload

    def _ask(self, scenes, full_text, style) -> dict:
        """One batch: the parsed answer. 429 -> wait + ask again (within the wait budget)."""
        s = get_settings()
        payload = self._payload(scenes, full_text, style)
        last_error: Exception | None = None
        tries = 0
        while tries < 1 + max(0, s.LLM_RETRIES):
            try:
                return self._call(payload)
            except RateLimited as e:
                wait = min(e.retry_after if e.retry_after is not None else 2.0, s.LLM_MAX_RETRY_AFTER_SECONDS)
                if self.waited + wait > s.LLM_MAX_WAIT_SECONDS:
                    raise UnderstandError(f"HTTP 429 (rate limit; waited {self.waited:.0f} s in total, "
                                          f"limit LLM_MAX_WAIT_SECONDS={s.LLM_MAX_WAIT_SECONDS:g})") from None
                log.warning("groq rate limit: waiting %.1f s (retry-after %s)", wait, e.retry_after)
                time.sleep(wait)
                self.waited += wait
            except (UnderstandError, httpx.HTTPError, ValueError) as e:
                last_error, tries = e, tries + 1
                log.warning("groq attempt %s failed: %s", tries, e)
                if tries < 1 + max(0, s.LLM_RETRIES):
                    time.sleep(1.0)
        raise UnderstandError(str(last_error) if not isinstance(last_error, httpx.HTTPError)
                              else type(last_error).__name__)

    def understand(self, scenes, full_text, style=None):
        """Meanings for the scenes the LLM answered. Raises UnderstandError only if it answered NONE of
        them; scenes it could not answer are listed in self.missing (the caller uses the rules for them)."""
        s = get_settings()
        if not s.GROQ_API_KEY:
            raise UnderstandError("GROQ_API_KEY is empty")
        size = max(1, s.LLM_BATCH_SIZE)
        answered: dict[int, SceneMeaning] = {}
        first_error: str | None = None
        for start in range(0, len(scenes), size):
            pending = list(scenes[start:start + size])
            for round_ in range(2):  # the batch, then once more for the scenes it missed
                try:
                    answer = self._ask(pending, full_text, style)
                except UnderstandError as e:
                    first_error = first_error or str(e)
                    for sc in pending:
                        self.missing[sc.index] = str(e)
                    break
                self.story_sensitive |= bool(isinstance(answer, dict) and answer.get("story_sensitive"))
                got, problems = self._parse(answer, pending)
                answered.update(got)
                pending = [sc for sc in pending if sc.index not in got]
                for sc in pending:
                    self.missing[sc.index] = problems.get(sc.index, f"the answer misses scene {sc.index}")
                if not pending:
                    break
                if round_ == 0:
                    log.warning("groq answer missed scenes %s; asking again for those only",
                                [sc.index for sc in pending])
            for sc in scenes[start:start + size]:
                if sc.index in answered:
                    self.missing.pop(sc.index, None)
        if not answered:
            raise UnderstandError(first_error or next(iter(self.missing.values()), "the answer has no scenes"))
        return [answered[sc.index] for sc in scenes if sc.index in answered]

    def _parse(self, answer: dict, scenes) -> tuple[dict[int, SceneMeaning], dict[int, str]]:
        """({scene index: meaning} for every usable scene answer, {scene index: problem} for unusable ones)."""
        items = {}
        for it in (answer.get("scenes") or []) if isinstance(answer, dict) else []:
            if isinstance(it, dict):
                try:
                    items[int(it.get("index", -1))] = it
                except (TypeError, ValueError):
                    continue
        out, problems = {}, {}
        for sc in scenes:
            item = items.get(sc.index)
            if item is None:
                problems[sc.index] = f"the answer misses scene {sc.index}"
                continue
            raw_queries = item.get("queries") or []
            if not isinstance(raw_queries, list):
                problems[sc.index] = f"scene {sc.index} has no query list"
                continue
            queries = [str(q).strip()[:80] for q in raw_queries if str(q).strip()]
            if not queries:
                problems[sc.index] = f"scene {sc.index} has no queries"
                continue
            roles = list(QUERY_ROLES[:len(queries)]) + ["extra"] * max(0, len(queries) - len(QUERY_ROLES))
            reason = "groq: " + str(item.get("reason") or "").strip()[:200]
            hint = (sc.visual_hint or "").strip()[:80]
            if hint:  # the caller's hint ALWAYS steers the search, even if the LLM left it out
                kept = [(q, r) for q, r in zip(queries, roles) if q.lower() != hint.lower()]
                queries, roles = [hint] + [q for q, _ in kept], ["visual_hint"] + [r for _, r in kept]
                reason += " (visual_hint used as the first query)"
            raw_flags = item.get("flags") if isinstance(item.get("flags"), dict) else {}
            facts = safety_facts(sc.text, english_of(sc))
            # Safety flags: the LLM's answer OR the code detectors (never weaker than the rules).
            flags = {name: bool(raw_flags.get(name)) for name in FLAG_NAMES}
            flags["statistic"] |= facts["number"] is not None
            flags["named_person"] |= bool(facts["people"])
            flags["brand"] |= bool(facts["brands"])
            flags["sensitive"] |= bool(facts["sensitive"])
            names = facts["people"] + facts["brands"]
            card = scene_card(sc.text, facts["number"], names)  # ALWAYS from the narration, by code
            subtitle, warning = check_card_suggestion(item.get("card"), sc.text, names)
            if warning:
                self.warnings.append(f"Scene {sc.index}: {warning}")
            if subtitle and card["number"]:  # a checked LLM subtitle may replace ours; the figure never changes
                card = {**card, "subtitle": subtitle, "subtitle_from": "llm, every word checked against the line"}
            unique = list(dict.fromkeys(zip(queries, roles)))
            seen, pairs = set(), []
            for q, r in unique:  # first QUERIES_PER_SCENE different queries, each with its role
                if q not in seen:
                    seen.add(q)
                    pairs.append((q, r))
            pairs = pairs[:max(1, get_settings().QUERIES_PER_SCENE)]
            out[sc.index] = SceneMeaning(index=sc.index, queries=[q for q, _ in pairs], flags=flags, source=self.name,
                                         reason=reason, names=names, number=facts["number"],
                                         keyword=card_keyword(sc.text, facts["number"], names), card=card,
                                         query_roles=[r for _, r in pairs])
        return out, problems


def check_card_suggestion(suggestion, scene_text: str, names: list[str]) -> tuple[str, str | None]:
    """An LLM's card idea {"headline", "subtitle"} -> (subtitle we may use or "", warning or None).

    The LLM never decides which number is shown. Its whole suggestion is dropped, with a warning, if
    it holds ANY number the narration does not say ("21%" for a line that says 40%). Its subtitle is
    used only if every word of it is in the scene's own text and it names no person or brand."""
    if not isinstance(suggestion, dict):
        return "", None
    shown = f"{str(suggestion.get('headline') or '')[:40]} {str(suggestion.get('subtitle') or '')[:60]}".strip()
    bad = cards.unverified_numbers(shown, scene_text)
    if bad:
        return "", (f"the LLM suggested card text '{shown}' with {', '.join(repr(b) for b in bad)}, which the "
                    "narration does not say; ignored (a card shows only figures from the narration)")
    subtitle = str(suggestion.get("subtitle") or "").strip()[:60]
    words, said = tokens(subtitle), set(tokens(scene_text))
    name_words = {w.lower() for n in names for w in n.split()}
    if not words or len(words) > cards.MAX_SUBTITLE_WORDS or any(w not in said or w in name_words for w in words):
        return "", None  # not a phrase from the line: keep the code's own subtitle
    return subtitle, None


ADAPTERS = {"groq": GroqUnderstander, "rules": RulesUnderstander, "mock": MockUnderstander}


def fingerprint(provider: str, scene, full_text: str, style: str | None) -> str:
    """A short hash of everything an answer depends on. Same fingerprint = the same question,
    so a stored answer can be reused instead of asking again."""
    s = get_settings()
    data = {"provider": provider, "text": scene.text, "visual_hint": scene.visual_hint, "script": full_text,
            "style": style, "card_rules": cards.CARD_RULES_VERSION, "queries_per_scene": s.QUERIES_PER_SCENE,
            "story_rule": [s.STORY_SENSITIVE_MIN_SCENES, s.STORY_SENSITIVE_MIN_SHARE]}
    if english_of(scene):  # only for non-English narration, so English fingerprints stay as they were
        data["english"] = english_of(scene)
    if provider == "groq":
        data.update(model=s.LLM_MODEL, prompt=prompt_version(), temperature=s.LLM_TEMPERATURE, seed=s.SEED)
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
        meanings, info = adapter.understand(scenes, full_text, style), adapter.info()
        warnings = list(getattr(adapter, "warnings", []))
    except UnderstandError as e:
        if provider != "groq":
            raise
        rules = RulesUnderstander()
        warning = f"Scene understanding with Groq failed ({e}); used the offline rules instead"
        meanings, info, warnings = (rules.understand(scenes, full_text, style), {**adapter.info(), "fallback": "rules"},
                                    [warning])
    missing = dict(getattr(adapter, "missing", {}) or {})
    if missing and len(meanings) < len(scenes):
        # Groq answered SOME scenes: the rules fill in only the ones it could not answer.
        rules, english_full = RulesUnderstander(), english_script(scenes, full_text)
        got = {m.index: m for m in meanings}
        for sc in scenes:
            if sc.index not in got:
                got[sc.index] = rules.understand_one(sc.index, sc.text, english_full, sc.visual_hint, english_of(sc))
                got[sc.index].reason += f" (Groq gave no usable answer for this scene: {missing.get(sc.index, '?')})"
        meanings = [got[sc.index] for sc in scenes]
        indexes = ", ".join(str(i) for i in sorted(missing))
        warnings.append(f"Scene understanding with Groq failed for scene(s) {indexes} "
                        f"({next(iter(missing.values()))}); used the offline rules for those scenes only")
        info = {**info, "fallback": "rules", "fallback_scenes": sorted(missing)}
    elif "fallback" in info:
        info = {**info, "fallback_scenes": [sc.index for sc in scenes]}
    else:
        info = {**info, "fallback_scenes": []}
    story_reason = apply_story_sensitivity(meanings, bool(getattr(adapter, "story_sensitive", False)))
    info["story_sensitive"] = bool(story_reason)
    if story_reason:
        info["story_sensitive_reason"] = story_reason
    for meaning, sc in zip(meanings, scenes):
        # Uses the provider that REALLY answered: a rules fallback is never reused as a Groq answer.
        meaning.fingerprint = fingerprint(meaning.source, sc, full_text, style)
    return meanings, info, warnings
