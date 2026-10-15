"""Safety rules: named people, brands, sensitive topics and the avoid list."""
from app.pipeline.safety import FACE_TERMS, GRAPHIC_TERMS, avoid_terms, find_excluded, plan_search
from app.pipeline.understand import RulesUnderstander, SceneMeaning


def meaning(queries, names=(), **flags):
    all_flags = {"abstract": False, "statistic": False, "named_person": False, "brand": False, "sensitive": False}
    return SceneMeaning(index=3, queries=list(queries), flags={**all_flags, **flags}, reason="test", source="test",
                        names=list(names))


def test_named_person_gets_generic_footage_no_faces_and_a_warning():
    plan = plan_search(meaning(["Elon Musk portrait", "rocket launch", "Musk's factory"], ["Elon Musk"],
                               named_person=True), "Elon Musk launched a rocket.")
    assert plan.no_faces is True
    assert all("musk" not in q.lower() for q in plan.queries)
    assert "portrait" not in " ".join(plan.queries)        # face query dropped
    assert "rocket launch" in plan.queries
    assert set(FACE_TERMS) <= set(plan.exclude_terms) and "elon musk" in plan.exclude_terms
    assert plan.warnings == ["Scene 3 mentions a named person (Elon Musk): using generic related footage with no faces"]


def test_named_person_from_rules_end_to_end():
    m = RulesUnderstander().understand_one(0, "Taylor Swift sold out the stadium again.", "x")
    plan = plan_search(m, "Taylor Swift sold out the stadium again.")
    assert plan.warnings and "Taylor Swift" in plan.warnings[0]
    assert all("swift" not in q.lower() and "taylor" not in q.lower() for q in plan.queries)


def test_brand_excludes_logos_and_warns():
    plan = plan_search(meaning(["Nike shoes", "running track"], ["Nike"], brand=True), "Nike shoes are popular.")
    assert "logo" in plan.exclude_terms and "nike" in plan.exclude_terms
    assert plan.queries[0] == "shoes"
    assert "brand (Nike)" in plan.warnings[0]


def test_sensitive_topic_adds_calm_modifier_and_blocks_graphic_terms():
    plan = plan_search(meaning(["white flowers at a memorial", "candle light"], sensitive=True), "He died.")
    assert all(q.startswith("calm ") for q in plan.queries)
    assert set(GRAPHIC_TERMS) <= set(plan.exclude_terms)
    assert find_excluded("rescue blood injury", plan.exclude_terms) == "blood"


def test_avoid_peoples_faces_excludes_faces_not_all_people():
    terms = avoid_terms(["people's faces"])
    assert {"face", "faces", "portrait", "close-up", "headshot", "selfie"} <= set(terms)
    assert "people" not in terms
    plan = plan_search(meaning(["portrait of a smiling woman", "city street", "close-up of a face"]), "x",
                       avoid=["people's faces"])
    assert plan.queries == ["city street"]
    assert plan.no_faces is True
    # Candidate tags are checked too:
    assert find_excluded("woman, portrait, smile", plan.exclude_terms) == "portrait"
    assert find_excluded("busy city street, cars", plan.exclude_terms) is None


def test_avoid_list_synonyms_and_plain_words():
    terms = avoid_terms(["Alcohol", "cars"])
    assert {"alcohol", "beer", "wine"} <= set(terms)
    assert {"cars", "car"} <= set(terms)
    assert find_excluded("a cold beer at the bar", terms) == "beer"
    assert find_excluded("scary carton", terms) is None  # whole words only


def test_all_queries_excluded_still_leaves_one_safe_query():
    plan = plan_search(meaning(["face close-up", "portrait"]), "Small shops open early every morning.",
                       avoid=["faces"])
    assert plan.queries and not find_excluded(plan.queries[0], plan.exclude_terms)
