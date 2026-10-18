"""Safety rules: turn a scene's meaning into a SAFE search plan.

Input: the SceneMeaning from understand.py + the request's `avoid` list.
Output: a SearchPlan = the queries to search, the words a clip must NOT have in its
tags/title (exclude terms), and warnings for the job.

Rules (from CLAUDE.md):
- named person / brand -> never search for the name, never show faces or logos: use
  generic related footage, and add a warning.
- sensitive topic        -> calm, respectful queries; graphic words are excluded.
- sensitive STORY        -> the same for EVERY scene of a sad / tragic script (understand.apply_story_sensitivity),
                            plus festive footage (party, fireworks ...) is excluded.
- avoid list             -> each item becomes exclude terms (with obvious synonyms), e.g.
                            "people's faces" -> face, portrait, close-up, headshot, selfie.
"""
import re
from dataclasses import dataclass, field

from app.pipeline.understand import SceneMeaning, content_words

FACE_TERMS = ["face", "faces", "portrait", "portraits", "close-up", "closeup", "close up", "headshot", "selfie",
              "person close-up"]
LOGO_TERMS = ["logo", "logos", "brand", "trademark"]
GRAPHIC_TERMS = ["blood", "bloody", "gore", "corpse", "dead body", "injury", "wound", "wounded", "weapon", "gun",
                 "guns", "knife", "explosion", "violence", "violent", "fight", "riot", "crash", "victim", "war zone"]
SENSITIVE_MODIFIER = "calm"
# Footage that would feel wrong anywhere in a sad story.
FESTIVE_TERMS = ["party", "parties", "celebration", "celebrating", "celebrate", "fireworks", "confetti", "dancing",
                 "disco", "carnival", "birthday"]

# Avoid-list words -> the extra terms they also block.
AVOID_SYNONYMS = {
    "face": FACE_TERMS,
    "alcohol": ["alcohol", "beer", "wine", "whisky", "whiskey", "vodka", "cocktail", "bar", "drunk"],
    "smoking": ["smoking", "smoke", "cigarette", "cigarettes", "tobacco", "cigar", "vape"],
    "weapon": ["weapon", "weapons", "gun", "guns", "rifle", "pistol", "knife", "knives"],
    "gun": ["gun", "guns", "rifle", "pistol", "weapon", "weapons"],
    "blood": ["blood", "bloody", "gore", "injury", "wound"],
    "child": ["child", "children", "kid", "kids", "baby", "toddler"],
    "people": ["people", "person", "man", "woman", "men", "women", "crowd"] + FACE_TERMS,
    "animal": ["animal", "animals", "dog", "cat", "pet", "wildlife"],
}


@dataclass
class SearchPlan:
    queries: list[str]
    exclude_terms: list[str]
    no_faces: bool = False
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # what safety changed, for timeline.json

    def to_dict(self) -> dict:
        return {"queries": self.queries, "exclude_terms": self.exclude_terms, "no_faces": self.no_faces,
                "notes": self.notes}


def avoid_terms(avoid: list[str] | None) -> list[str]:
    """["people's faces", "Alcohol"] -> ["people's faces", "face", "faces", "portrait", ..., "alcohol", "beer", ...]"""
    terms: list[str] = []
    for item in avoid or []:
        phrase = item.strip().lower()
        if not phrase:
            continue
        words = [w.strip("'’") for w in re.findall(r"[a-z'’\-]+", phrase)]
        # "people's faces" means faces, not all people: the face rule wins.
        keys = ["face"] if any(w.startswith("face") for w in words) else words
        matched = False
        for word in keys:
            base = word[:-1] if word.endswith("s") and word[:-1] in AVOID_SYNONYMS else word
            if base in AVOID_SYNONYMS:
                terms += AVOID_SYNONYMS[base]
                matched = True
        if not matched:
            terms.append(phrase)
            if phrase.endswith("s") and len(phrase) > 3:
                terms.append(phrase[:-1])  # "cars" also blocks "car"
            else:
                terms.append(phrase + "s")
    return list(dict.fromkeys(terms))


def find_excluded(text: str, terms: list[str]) -> str | None:
    """The first exclude term found in text as a whole word/phrase, or None."""
    lowered = text.lower()
    for term in terms:
        if re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", lowered):
            return term
    return None


def _remove_names(query: str, names: list[str]) -> str:
    for name in names:
        for word in name.split():
            query = re.sub(rf"\b{re.escape(word)}('s|’s)?\b", "", query, flags=re.I)
    return re.sub(r"\s+", " ", query).strip()


def plan_search(meaning: SceneMeaning, scene_text: str, avoid: list[str] | None = None) -> SearchPlan:
    flags = meaning.flags
    queries = list(meaning.queries)
    exclude = avoid_terms(avoid)
    warnings: list[str] = []
    notes: list[str] = []
    no_faces = bool(set(FACE_TERMS) & set(exclude))

    if flags.get("named_person") or flags.get("brand"):
        queries = [_remove_names(q, meaning.names) for q in queries]
        exclude += [n.lower() for n in meaning.names]
    if flags.get("named_person"):
        no_faces = True
        exclude += FACE_TERMS
        who = ", ".join(meaning.names) or "a real person"
        warnings.append(f"Scene {meaning.index} mentions a named person ({who}): "
                        "using generic related footage with no faces")
        notes.append("named person: no faces, name removed from queries")
    if flags.get("brand"):
        exclude += LOGO_TERMS
        brands = ", ".join(meaning.names) or "a brand"
        warnings.append(f"Scene {meaning.index} mentions a brand ({brands}): using generic footage without logos")
        notes.append("brand: logos excluded, brand removed from queries")
    if flags.get("sensitive") or meaning.story_sensitive:
        queries = [q if q.lower().startswith(SENSITIVE_MODIFIER) else f"{SENSITIVE_MODIFIER} {q}" for q in queries]
        exclude += GRAPHIC_TERMS
        if flags.get("sensitive"):
            notes.append("sensitive topic: calm queries, graphic content excluded")
        else:
            notes.append("sensitive story: calm queries, graphic content excluded (the line alone looks neutral)")
    if meaning.story_sensitive:
        exclude += FESTIVE_TERMS
        notes.append("sensitive story: festive footage excluded")

    # Drop queries that ask for something excluded; never end up with no query at all.
    kept = [q for q in queries if q and not find_excluded(q, exclude)]
    if len(kept) < len(queries):
        notes.append(f"dropped {len(queries) - len(kept)} queries that matched exclude terms")
    if not kept:
        words = [w for w in content_words(scene_text) if not find_excluded(w, exclude)
                 and w not in {x.lower() for n in meaning.names for x in n.split()}]
        kept = [" ".join(words[:3]) or "abstract background light"]
    kept = list(dict.fromkeys(kept))
    return SearchPlan(queries=kept, exclude_terms=list(dict.fromkeys(exclude)), no_faces=no_faces,
                      warnings=warnings, notes=notes)
