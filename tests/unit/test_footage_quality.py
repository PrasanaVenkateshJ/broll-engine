"""Session 7B: real footage instead of plain cards, and a selection rule that can be re-checked.

- statistics: real footage + the narration's number as a badge (overlay), a card only as the last resort
- a stock VIDEO is preferred over photo-motion within PREFER_VIDEO_MARGIN
- adaptive threshold: a near miss is used only when the independent judge agrees
- the VLM judge re-ranks / drops candidates (mock judge + a fake Groq server)
- text-heavy penalty, extra frames per candidate
- Openverse: strict licence filter; Pixabay: one bigger request, isAiGenerated, rate limit + cooldown
- crop near-tie: the real run-1 sailboat case keeps the boat whole
"""
import base64
import json
import time
from pathlib import Path

import pytest
from PIL import Image

from app.config import get_settings
from app.pipeline import vlm
from app.pipeline.providers import REGISTRY, get_provider, provider_status
from app.pipeline.providers import base as provider_base
from app.pipeline.providers.base import Candidate, ProviderError
from app.pipeline.providers.mock import MockProvider
from app.pipeline.providers.openverse import check_licence, parse_results
from app.pipeline.rank import (MockScorer, Row, SelectionContext, _multi_frame, adaptive_floor, get_scorer,
                               select_clip, select_clips)
from app.pipeline.render import cuts_subject, pick_crop, render_segment, analyse_source, subject_span, write_badge_ass
from app.pipeline.safety import plan_search
from app.pipeline.scenes import Scene
from app.pipeline.transcribe import Word
from app.pipeline.tts import MockTTS, synthesize_cached
from app.pipeline.understand import MockUnderstander
from tests.fake_api import FakeAPI
from tests.media_helpers import make_video

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def setenv(monkeypatch, **values):
    for key, value in values.items():
        monkeypatch.setenv(key, str(value))
    get_settings.cache_clear()


def make(texts):
    scenes = [Scene(i, i * 4.0, i * 4.0 + 4.0, t) for i, t in enumerate(texts)]
    meanings = MockUnderstander().understand(scenes, " ".join(texts))
    return scenes, meanings, [plan_search(m, sc.text) for m, sc in zip(meanings, scenes)]


def context(tmp_path, warnings=None, judge=None):
    return SelectionContext(work=tmp_path, orientation="landscape", warn=(warnings if warnings is not None else []).append,
                            scorer=get_scorer(), judge=judge)


class Scored(MockProvider):
    """A mock source whose candidates get FIXED scores: SCORES[rank] (+ optional judge ratings / text probs)."""
    name = "vid"
    SCORES = [0.23, 0.22]
    VLM: list[int] = []
    TEXT: list[float] = []
    searches = 0

    def search(self, query, orientation, min_duration):
        type(self).searches += 1
        out = []
        for i, score in enumerate(self.SCORES):
            c = self._candidate(cid(self.name, i), query, i)  # hex ids: the mock clip colour comes from the id
            c.extra["score"] = score
            if i < len(self.VLM):
                c.extra["vlm_score"] = self.VLM[i]
            if i < len(self.TEXT):
                c.extra["text_prob"] = self.TEXT[i]
            out.append(c)
        return out


def cid(name, i):
    return ("a" if name == "vid" else "b") + f"{i:09d}"


class Photo(Scored):
    name = "photo"
    media_kind = "image"
    SCORES = [0.30]


@pytest.fixture
def two_sources(monkeypatch):
    for cls in (Scored, Photo):
        monkeypatch.setattr(cls, "searches", 0)
    monkeypatch.setitem(REGISTRY, "vid", Scored)
    monkeypatch.setitem(REGISTRY, "photo", Photo)
    setenv(monkeypatch, PROVIDER_ORDER="vid,photo,generated")


LINE = "Small shops open early every morning."


# ---------- Statistics: footage + badge ----------

def test_statistic_line_gets_real_footage_with_the_number_as_a_badge(tmp_path):
    scenes, meanings, plans = make(["In 2025, household savings grew by 40 percent."])
    sel = select_clip(scenes[0], meanings[0], plans[0], context(tmp_path))
    assert sel.candidate.provider == "mock" and not sel.candidate.generated
    assert sel.overlay["headline"] == "40%" and sel.overlay["source"] == "narration"
    assert sel.overlay["subtitle"] == "household savings grew"
    assert sel.reason.startswith("statistic line on real footage: number badge '40%'")


def test_statistic_line_falls_back_to_a_number_card_when_no_footage_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(MockProvider, "fail", True)
    scenes, meanings, plans = make(["In 2025, household savings grew by 40 percent."])
    sel = select_clip(scenes[0], meanings[0], plans[0], context(tmp_path))
    assert sel.candidate.generated and sel.candidate.extra["headline"] == "40%" and sel.overlay is None
    assert sel.reason.startswith("statistic line: no stock clip passed the rule")


def test_the_badge_shows_only_the_narrations_number(tmp_path):
    ass = write_badge_ass({"headline": "40%", "subtitle": "household savings grew"}, 1920, 1080, 4.0,
                          tmp_path / "b.ass").read_text(encoding="utf-8")
    dialogue = [line for line in ass.splitlines() if line.startswith("Dialogue")]
    assert "40%" in dialogue[0] and "\\fad(" in dialogue[0] and "household savings grew" in dialogue[1]
    shown = " ".join(line.split(",,0,0,0,,", 1)[1].split("}", 1)[1] for line in dialogue)
    assert [ch for ch in shown if ch.isdigit()] == ["4", "0"]  # no other number is ever drawn


def test_a_segment_with_a_badge_renders_and_records_it(tmp_path):
    clip = make_video(tmp_path / "c.mp4", seconds=3.0)
    sel = Selection_with_overlay(clip)
    scene = Scene(0, 0.0, 2.0, "Savings grew by 40 percent.", [Word("Savings", 0.1, 0.5)])
    info = analyse_source(sel, 2.0)
    path, facts = render_segment(info, scene, "16:9", [], tmp_path, 60, False)
    assert path.is_file() and facts["overlay"] == {"headline": "40%", "subtitle": "savings grew", "source": "narration"}


def Selection_with_overlay(path):
    from app.pipeline.rank import Selection
    cand = Candidate(provider="test", id="ov", kind="video", width=640, height=360)
    return Selection(0, cand, path, 0.3, 0.3, "test", overlay={"headline": "40%", "subtitle": "savings grew"})


# ---------- Video first, within PREFER_VIDEO_MARGIN ----------

def test_a_photo_wins_only_when_it_beats_the_video_by_more_than_the_margin(tmp_path, monkeypatch, two_sources):
    monkeypatch.setattr(Scored, "SCORES", [0.23])
    monkeypatch.setattr(Photo, "SCORES", [0.30])   # 0.30 > 0.23 + 0.03
    scenes, meanings, plans = make([LINE])
    sel = select_clip(scenes[0], meanings[0], plans[0], context(tmp_path))
    assert sel.candidate.provider == "photo" and "photo-motion preferred" in sel.reason


def test_a_video_within_the_margin_is_preferred(tmp_path, monkeypatch, two_sources):
    monkeypatch.setattr(Scored, "SCORES", [0.23])
    monkeypatch.setattr(Photo, "SCORES", [0.25])   # not more than 0.03 above
    scenes, meanings, plans = make([LINE])
    sel = select_clip(scenes[0], meanings[0], plans[0], context(tmp_path))
    assert sel.candidate.provider == "vid" and "video preferred over photo-motion" in sel.reason


def test_a_confident_video_skips_the_photo_search(tmp_path, monkeypatch, two_sources):
    monkeypatch.setattr(Scored, "SCORES", [0.28])  # >= 0.22 + 0.03
    scenes, meanings, plans = make([LINE])
    sel = select_clip(scenes[0], meanings[0], plans[0], context(tmp_path))
    assert sel.candidate.provider == "vid" and Photo.searches == 0
    assert "photo sources not searched" in sel.reason


# ---------- Text-heavy penalty ----------

def test_a_text_heavy_picture_loses_a_near_tie(tmp_path, monkeypatch, two_sources):
    monkeypatch.setattr(Scored, "SCORES", [0.30, 0.29])
    monkeypatch.setattr(Scored, "TEXT", [0.9, 0.0])  # the best one looks like a slide full of text
    scenes, meanings, plans = make([LINE])
    sel = select_clip(scenes[0], meanings[0], plans[0], context(tmp_path))
    assert sel.candidate.id == cid("vid", 1) and sel.details["text_penalty"] == 0.0


# ---------- Adaptive threshold + the independent judge ----------

def test_without_a_judge_a_near_miss_is_not_used(tmp_path, monkeypatch, two_sources):
    monkeypatch.setattr(Scored, "SCORES", [0.20])
    monkeypatch.setattr(Photo, "SCORES", [0.19])
    setenv(monkeypatch, BEST_EFFORT_MIN_SCORE=0)  # the adaptive pass alone (best-effort footage tested elsewhere)
    scenes, meanings, plans = make([LINE])
    sel = select_clip(scenes[0], meanings[0], plans[0], context(tmp_path))
    assert sel.candidate.generated and "adaptive pass off" in sel.reason


def test_a_near_miss_is_used_when_the_judge_rates_it_highly(tmp_path, monkeypatch, two_sources):
    monkeypatch.setattr(Scored, "SCORES", [0.20])  # below 0.22, above 0.22 x 0.88 = 0.1936
    monkeypatch.setattr(Scored, "VLM", [4])
    monkeypatch.setattr(Photo, "SCORES", [0.10])
    setenv(monkeypatch, VLM_PROVIDER="mock")
    scenes, meanings, plans = make([LINE])
    selections, facts = select_clips(scenes, meanings, plans, context(tmp_path, judge=vlm.get_judge()))
    sel = selections[0]
    assert sel.candidate.id == cid("vid", 0) and sel.details["adaptive"] is True
    assert sel.reason.startswith("adaptive threshold") and "rated it 4/5" in sel.reason
    assert sel.vlm["score"] == 4 and sel.vlm["priority"] == "near miss (adaptive pass)"
    assert adaptive_floor() == pytest.approx(0.1936)
    assert facts["vlm"]["scenes_judged"] == 1


def test_a_near_miss_the_judge_dislikes_is_not_used(tmp_path, monkeypatch, two_sources):
    monkeypatch.setattr(Scored, "SCORES", [0.20])
    monkeypatch.setattr(Scored, "VLM", [2])
    monkeypatch.setattr(Photo, "SCORES", [0.10])
    setenv(monkeypatch, VLM_PROVIDER="mock", BEST_EFFORT_MIN_SCORE=0)
    scenes, meanings, plans = make([LINE])
    selections, _ = select_clips(scenes, meanings, plans, context(tmp_path, judge=vlm.get_judge()))
    assert selections[0].candidate.generated and "rated the near-miss candidates below 4/5" in selections[0].reason


def test_the_judge_re_ranks_and_drops_candidates(tmp_path, monkeypatch, two_sources):
    setenv(monkeypatch, PROVIDER_ORDER="vid,generated", VLM_PROVIDER="mock", QUALITY_MODE="best")
    monkeypatch.setattr(Scored, "SCORES", [0.30, 0.29, 0.28])
    monkeypatch.setattr(Scored, "VLM", [1, 3, 5])  # CLIP's favourite is rated unrelated
    scenes, meanings, plans = make([LINE])
    selections, _ = select_clips(scenes, meanings, plans, context(tmp_path, judge=vlm.get_judge()))
    sel = selections[0]
    assert sel.candidate.id == cid("vid", 2) and sel.vlm["score"] == 5
    assert "VLM judge 5/5" in sel.reason and f"dropped vid:{cid('vid', 0)}" in sel.reason


def test_the_judge_budget_goes_to_the_most_uncertain_scenes(tmp_path, monkeypatch, two_sources):
    setenv(monkeypatch, PROVIDER_ORDER="vid,generated", VLM_PROVIDER="mock", VLM_MAX_SCENES_PER_JOB=1, QUALITY_MODE="best")
    monkeypatch.setattr(Scored, "SCORES", [0.30, 0.29, 0.28, 0.27, 0.26, 0.24, 0.23, 0.225])
    scenes, meanings, plans = make([LINE, "Busy streets at night."])
    ctx = context(tmp_path, judge=vlm.get_judge())
    select_clips(scenes, meanings, plans, ctx)
    assert sum("scores" in v for v in ctx.vlm.values()) == 1
    assert any("VLM_MAX_SCENES_PER_JOB" in v.get("skipped", "") for v in ctx.vlm.values())


# ---------- More frames per candidate ----------

class FrameScorer(MockScorer):
    needs_images = True

    def score(self, candidates, previews, texts):
        return [0.40, 0.10][:len(candidates)]


class Prober:
    def probe_frames(self, candidate, dest_dir, positions):
        dest_dir.mkdir(parents=True, exist_ok=True)
        return [dest_dir / f"{candidate.id}_{i}.jpg" for i in range(len(positions))]


def test_the_best_videos_are_scored_on_thumbnail_plus_two_frames(tmp_path):
    rows = [Row(Candidate("p", f"c{i}", "video"), score, 0.0, None, i) for i, score in enumerate([0.30, 0.25, 0.10])]
    ctx = SelectionContext(work=tmp_path, orientation="landscape", warn=print, scorer=FrameScorer())
    _multi_frame(Prober(), rows, ["q"], ctx)
    assert rows[0].frame_scores == [0.30, 0.40, 0.10] and rows[0].score == pytest.approx(0.2667, abs=1e-4)
    assert rows[0].thumb_score == 0.30 and rows[0].details()["frames_scored"] == 3
    assert rows[2].frame_scores is None  # below the adaptive floor: not worth a download


# ---------- Openverse: strict licences ----------

@pytest.mark.parametrize("code, version, allowed", [
    ("cc0", "1.0", True), ("pdm", "1.0", True), ("by", "2.0", True), ("by-sa", "4.0", False),
    ("by-nc", "2.0", False), ("by-nd", "4.0", False), ("by-nc-sa", "2.0", False), ("sampling+", "1.0", False),
    ("", "", False)])
def test_openverse_licence_filter(code, version, allowed):
    assert check_licence(code, version)[0] is allowed


def openverse_item(i, licence="by", creator="Jane", mature=False, url="https://creativecommons.org/licenses/by/2.0/"):
    return {"id": f"0000-{i}", "title": f"Piggy {i}", "license": licence, "license_version": "2.0",
            "license_url": url, "creator": creator, "mature": mature, "url": f"https://img.example/{i}.jpg",
            "thumbnail": f"https://api.openverse.org/v1/images/0000-{i}/thumb/", "foreign_landing_url": "https://f/",
            "width": 1024, "height": 680, "source": "flickr", "tags": [{"name": "piggy bank"}]}


def test_openverse_keeps_only_safe_licences_with_credit():
    data = {"results": [openverse_item(1), openverse_item(2, licence="by-sa"), openverse_item(3, creator=""),
                        openverse_item(4, mature=True), openverse_item(5, url="https://example.com/licence"),
                        openverse_item(6, licence="cc0", creator="",
                                       url="https://creativecommons.org/publicdomain/zero/1.0/")]}
    found, rejected = parse_results(data, "piggy bank")
    assert [c.id for c in found] == ["0000-1", "0000-6"] and len(rejected) == 4
    assert found[0].licence_name == "CC BY 2.0" and "Jane" in found[0].attribution
    assert "creativecommons.org/licenses/by/2.0" in found[0].attribution and found[0].kind == "image"


def test_openverse_search_asks_for_safe_licences_and_two_queries_at_most(monkeypatch):
    server = FakeAPI(lambda *a: (200, {"results": [openverse_item(1)]}))
    try:
        setenv(monkeypatch, OPENVERSE_API_URL=server.url + "/v1/images/", QUALITY_MODE="best")
        provider = get_provider("openverse")
        provider.search("piggy bank", "landscape", 4.0)
    finally:
        server.close()
    query = server.calls[0]["query"]
    assert query["license"] == "cc0,pdm,by" and query["page_size"] == "16" and query["mature"] == "false"
    assert provider.max_queries(["a", "b", "c", "d", "e"]) == ["a", "b"]


# ---------- Pixabay: one bigger request, AI flag, small rendition, rate limit ----------

def pixabay_hit(ai=False):
    videos = {k: {"url": f"https://cdn.example/1_{k}.mp4", "width": w, "height": h, "size": 1000,
                  "thumbnail": f"https://cdn.example/1_{k}.jpg"}
              for k, w, h in (("large", 3840, 2160), ("medium", 1920, 1080), ("small", 1280, 720), ("tiny", 960, 540))}
    return {"id": 1, "pageURL": "https://pixabay.com/videos/id-1/", "tags": "coins", "duration": 12, "user": "U",
            "isAiGenerated": ai, "videos": videos}


def test_pixabay_asks_two_pages_in_one_request_and_records_the_ai_flag(monkeypatch):
    server = FakeAPI(lambda *a: (200, {"hits": [pixabay_hit(ai=True)]}))
    try:
        setenv(monkeypatch, PIXABAY_API_KEY="k", PIXABAY_VIDEO_API_URL=server.url + "/api/videos/", QUALITY_MODE="best")
        found = get_provider("pixabay_video").search("coins", "landscape", 4.0)
    finally:
        server.close()
    assert server.calls[0]["query"]["per_page"] == "16" and len(server.calls) == 1  # 8 results x 2 pages
    assert found[0].ai_generated is True and found[0].extra["probe_url"].endswith("_tiny.mp4")


def test_pixabay_429_puts_both_pixabay_sources_on_a_cooldown(monkeypatch):
    server = FakeAPI(lambda *a: (429, {"error": "too many"}, {"retry-after": "120"}))
    try:
        setenv(monkeypatch, PIXABAY_API_KEY="k", PIXABAY_VIDEO_API_URL=server.url + "/api/videos/")
        with pytest.raises(ProviderError, match="HTTP 429"):
            get_provider("pixabay_video").search("coins", "landscape", 4.0)
        with pytest.raises(ProviderError, match="cooling down"):
            get_provider("pixabay_video").search("coins", "landscape", 4.0)
    finally:
        server.close()
    assert len(server.calls) == 1  # the second search never reached the API
    assert "cooling down" in provider_status("pixabay_video") and "cooling down" in provider_status("pixabay_image_motion")


def test_the_client_request_limit_waits_instead_of_failing(monkeypatch):
    clock = {"t": 1000.0}
    waits = []
    monkeypatch.setattr(provider_base.time, "monotonic", lambda: clock["t"])

    def sleep(seconds):
        waits.append(seconds)
        clock["t"] += seconds
    monkeypatch.setattr(provider_base.time, "sleep", sleep)
    server = FakeAPI(lambda *a: (200, {"hits": []}))
    try:
        setenv(monkeypatch, PIXABAY_API_KEY="k", PIXABAY_VIDEO_API_URL=server.url + "/v/",
               PROVIDER_REQUESTS_PER_MINUTE="pixabay=2")
        for q in ("a", "b", "c"):
            get_provider("pixabay_video").search(q, "landscape", 4.0)
    finally:
        server.close()
    assert len(server.calls) == 3 and len(waits) == 1 and 59 < waits[0] <= 61


# ---------- The Groq vision judge (fake server) ----------

def picture(path, colour):
    Image.new("RGB", (320, 180), colour).save(path)
    return path


def test_groq_judge_sends_one_contact_sheet_and_reads_the_ratings(tmp_path, monkeypatch):
    content = {"ratings": {"A": 5, "B": 1}, "why": "A shows coins in a jar"}
    server = FakeAPI(lambda *a: (200, {"choices": [{"message": {"content": json.dumps(content)}}],
                                       "usage": {"total_tokens": 900}}))
    items = [("p:a", Candidate("p", "a", "video"), picture(tmp_path / "a.jpg", "red")),
             ("p:b", Candidate("p", "b", "video"), picture(tmp_path / "b.jpg", "blue"))]
    try:
        setenv(monkeypatch, VLM_PROVIDER="groq", GROQ_API_KEY="k", GROQ_BASE_URL=server.url)
        judge = vlm.get_judge()
        scores, why = judge.judge("Savings grew by 40 percent.", None, items)
    finally:
        server.close()
    assert scores == {"p:a": 5, "p:b": 1} and "coins" in why
    body = json.loads(server.calls[0]["body"])
    parts = body["messages"][1]["content"]
    assert body["model"] == "qwen/qwen3.8-27b" and body["temperature"] == 0
    assert sum(p["type"] == "image_url" for p in parts) == 1           # ONE picture for all candidates
    sheet = Image.open(__import__("io").BytesIO(base64.b64decode(parts[1]["image_url"]["url"].split(",", 1)[1])))
    assert sheet.size == (2 * 256, 144)
    assert judge.info()["tokens_used"] == 900


def test_a_long_rate_limit_pauses_the_judge_without_failing(tmp_path, monkeypatch):
    server = FakeAPI(lambda *a: (429, {"error": "daily limit"}, {"retry-after": "3600"}))
    items = [("p:a", Candidate("p", "a", "video"), picture(tmp_path / "a.jpg", "red"))]
    try:
        setenv(monkeypatch, VLM_PROVIDER="groq", GROQ_API_KEY="k", GROQ_BASE_URL=server.url)
        with pytest.raises(vlm.JudgeUnavailable):
            vlm.get_judge().judge("x", None, items)
        with pytest.raises(vlm.JudgeUnavailable, match="paused"):
            vlm.get_judge().judge("x", None, items)
    finally:
        server.close()
    assert len(server.calls) == 1 and vlm.disabled_for() > 3000


# ---------- Crop near-tie: the run-1 sailboat ----------

def test_the_sailboat_near_tie_keeps_the_boat_whole():
    data = json.loads((FIXTURES / "sailboat_360429_crop.json").read_text(encoding="utf-8"))
    crop, s = data["crop"], get_settings()
    windows = {name: w["start"] for name, w in crop["windows"].items()}
    scores = {name: w["score"] for name, w in crop["windows"].items()}
    span = subject_span(data["row_local"], s.CROP_SUBJECT_LEVEL, s.CROP_SUBJECT_PAD)
    assert 0.44 < span[0] < 0.50 and 0.56 < span[1] < 0.62   # the boat: mast top ~0.44, hull ~0.56 (frame check)
    cut = {name for name, start in windows.items() if cuts_subject(start, crop["window_fraction"], span)}
    assert cut == {"salient"}                                # 0.529 starts inside the boat
    old = pick_crop(scores, crop["full_frame_score"], s.CROP_BLUR_MARGIN, s.CROP_TIE_MARGIN)
    new = pick_crop(scores, crop["full_frame_score"], s.CROP_BLUR_MARGIN, s.CROP_TIE_MARGIN, cut)
    assert (old, new) == ("salient", "centre")               # run 1 cut the boat; now the centre keeps it


def test_a_clear_winner_is_still_taken_even_if_it_cuts():
    assert pick_crop({"salient": 0.30, "centre": 0.20}, 0.29, 0.02, 0.03, cut={"salient"}) == "salient"
    assert pick_crop({"salient": 0.30, "centre": 0.29}, 0.29, 0.02, 0.03, cut={"salient", "centre"}) == "salient"


# ---------- TTS cache ----------

def test_the_same_text_is_spoken_only_once(tmp_path, monkeypatch):
    calls = []
    real = MockTTS.synthesize
    monkeypatch.setattr(MockTTS, "synthesize", lambda self, *a: calls.append(a[0]) or real(self, *a))
    tts = MockTTS()
    first, hit1 = synthesize_cached(tts, "Hello there.", "en-US-female", tmp_path / "a.wav")
    second, hit2 = synthesize_cached(tts, "Hello there.", "en-US-female", tmp_path / "b.wav")
    _, hit3 = synthesize_cached(tts, "Hello again.", "en-US-female", tmp_path / "c.wav")
    assert (hit1, hit2, hit3) == (False, True, False) and calls == ["Hello there.", "Hello again."]
    assert (tmp_path / "a.wav").read_bytes() == (tmp_path / "b.wav").read_bytes()


# ---------- Speed changes that must not change the choice ----------

def test_prefetching_the_likely_winner_gives_the_same_choices(tmp_path, monkeypatch):
    texts = [LINE, "Busy streets at night.", "Small shops open early every morning.", "Cloud computing helps."]
    results = []
    for flag in ("true", "false"):
        setenv(monkeypatch, PREFETCH_CLIPS=flag)
        scenes, meanings, plans = make(texts)
        ctx = context(tmp_path / flag)
        selections, _ = select_clips(scenes, meanings, plans, ctx)
        results.append([(s.candidate.uid, s.clip_score, s.final_score) for s in selections])
        if flag == "true":
            assert ctx.prefetched == {}  # every prefetched clip was used by pass 2
    assert results[0] == results[1]


def test_candidates_are_capped_round_robin_over_the_queries(tmp_path, monkeypatch, two_sources):
    setenv(monkeypatch, PROVIDER_ORDER="vid,generated", MAX_CANDIDATES_PER_SOURCE=3)
    monkeypatch.setattr(Scored, "SCORES", [0.30, 0.29, 0.28, 0.27])
    from app.pipeline.rank import get_ranking
    scenes, meanings, plans = make([LINE])
    ranking = get_ranking(get_provider("vid"), scenes[0], plans[0], context(tmp_path))
    assert len(ranking.rows) == 3  # 4 results x several queries, but only 3 scored
    assert ranking.found == 4      # the same ids come back for every query (merged without duplicates)


def test_a_source_whose_passing_clips_the_judge_dropped_says_so(tmp_path, monkeypatch, two_sources):
    setenv(monkeypatch, PROVIDER_ORDER="vid,generated", VLM_PROVIDER="mock", QUALITY_MODE="best")
    monkeypatch.setattr(Scored, "SCORES", [0.30])
    monkeypatch.setattr(Scored, "VLM", [1])
    scenes, meanings, plans = make([LINE])
    selections, _ = select_clips(scenes, meanings, plans, context(tmp_path, judge=vlm.get_judge()))
    assert selections[0].candidate.generated
    assert "passed 0.22 (best 0.300) but the VLM judge dropped them" in selections[0].reason
