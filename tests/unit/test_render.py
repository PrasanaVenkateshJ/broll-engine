"""Rendering one segment: exact length, size, fps, pixel format, salient crop, slow/loop, cards."""
from fractions import Fraction

import numpy as np
import pytest

from app.config import get_settings
from app.pipeline import media, qa
from app.pipeline.providers.base import Candidate
from app.pipeline.providers.generated import GeneratedProvider
from app.pipeline.rank import Selection
from app.pipeline.render import (SourceInfo, analyse_source, choose_window, look_filter, pick_crop,
                                 plan_framing, render_segment, time_plan)
from app.pipeline.scenes import Scene
from app.pipeline.transcribe import Word
from tests.media_helpers import make_image, make_subject_on_right, make_subjects_at_both_edges, make_video


def selection_for(path, kind="video", width=640, height=360, uid="t"):
    cand = Candidate(provider="test", id=uid, kind=kind, width=width, height=height)
    return Selection(0, cand, path, 0.3, 0.3, "test")


def video_facts(path):
    info = media.probe(path)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    return v["codec_name"], v["pix_fmt"], int(v["width"]), int(v["height"]), Fraction(v["avg_frame_rate"]), \
        int(v["nb_frames"])


def scene(seconds, words=True):
    w = [Word("Hello", 0.2, 0.6), Word("world.", 0.7, 1.2)] if words else []
    return Scene(0, 0.0, seconds, "Hello world.", w)


# ---------- Pure decisions ----------

def test_time_plan():
    assert time_plan(10.0, 5.0, 0.25) == ("direct", 1.0)
    assert time_plan(4.5, 5.0, 0.25) == ("slow", round(5.1 / 4.5, 4))     # 13 % slower: fine
    assert time_plan(1.5, 5.0, 0.25) == ("loop", 1.25)                     # too short: slow 25 % + loop


def test_choose_window_finds_the_subject():
    profile = np.ones(100)
    profile[80:95] = 20  # busy area on the right
    start, centre, mode = choose_window(profile, 0.3)
    assert mode == "salient" and 0.6 < start <= 0.7 and 0.8 < centre < 0.95


def test_choose_window_even_energy_uses_the_centre():
    assert choose_window(np.ones(100), 0.3) == (0.35, 0.5, "center")


def test_choose_window_subject_wider_than_crop_uses_blurred_fit():
    profile = np.ones(100)
    profile[0:10] = 20
    profile[90:100] = 20  # two subjects at both edges
    assert choose_window(profile, 0.3)[2] == "blur"


def test_look_filter_brightens_dark_clips_but_only_a_little():
    s = get_settings()
    dark = SourceInfo(kind="video", path=None, luma=40.0, saturation=s.TARGET_SATURATION)
    assert look_filter(dark) == f"eq=brightness={s.MAX_BRIGHTNESS_SHIFT:.3f}"
    normal = SourceInfo(kind="video", path=None, luma=s.TARGET_LUMA, saturation=s.TARGET_SATURATION)
    assert look_filter(normal) is None
    grey = SourceInfo(kind="video", path=None, luma=s.TARGET_LUMA, saturation=s.TARGET_SATURATION / 3)
    assert look_filter(grey) == "eq=saturation=1.200"  # colour boost is capped at +20 %


# ---------- Real renders ----------

def test_9x16_crop_follows_the_subject(tmp_path):
    clip = make_subject_on_right(tmp_path / "subject.mp4", 3.0)
    info = analyse_source(selection_for(clip), 2.0)
    framing = plan_framing(info, 1080, 1920)
    assert framing.mode == "salient"
    assert framing.x > 0.5  # crop window moved to the right, where the subject is (not blindly the centre)
    assert info.luma is not None and info.saturation is not None  # measured with signalstats


@pytest.mark.parametrize("ratio, size", [("16:9", (1920, 1080)), ("9:16", (1080, 1920)), ("1:1", (1080, 1080))])
def test_segment_has_exact_frames_size_fps_and_pixel_format(tmp_path, ratio, size):
    clip = make_video(tmp_path / "clip.mp4", 4.0)
    info = analyse_source(selection_for(clip), 2.0)
    path, facts = render_segment(info, scene(2.0), ratio, [], tmp_path, 60, burn_captions=False)
    assert video_facts(path) == ("h264", "yuv420p", *size, 30, 60)
    assert facts["timing"] == "trimmed" and facts["cache_key"] in path.name


def test_short_clip_is_slowed_down_not_frozen(tmp_path):
    clip = make_video(tmp_path / "short.mp4", 4.5)
    info = analyse_source(selection_for(clip), 5.0)
    path, facts = render_segment(info, scene(5.0), "16:9", [], tmp_path, 150, burn_captions=False)
    assert facts["timing"].startswith("slowed")
    assert video_facts(path)[-1] == 150
    assert qa.check_segment(path, 150) == []


def test_very_short_clip_loops_forward_and_backward_without_freezing(tmp_path):
    clip = make_video(tmp_path / "tiny.mp4", 1.5)
    info = analyse_source(selection_for(clip), 5.0)
    path, facts = render_segment(info, scene(5.0), "9:16", [], tmp_path, 150, burn_captions=False)
    assert facts["timing"] == "slowed 1.25x + ping-pong loop"
    assert video_facts(path)[-1] == 150
    assert qa.check_segment(path, 150) == []  # no frozen picture, no black frames


def test_still_image_gets_ken_burns_motion(tmp_path):
    image = make_image(tmp_path / "photo.jpg", "1280x720")
    info = analyse_source(selection_for(image, kind="image", width=1280, height=720), 3.0)
    path, facts = render_segment(info, scene(3.0), "9:16", [], tmp_path, 90, burn_captions=True)
    assert facts["timing"].startswith("ken burns")
    # A JPEG is full-range "yuvj420p"; the segment must still be normal yuv420p (limited range).
    assert video_facts(path)[:4] == ("h264", "yuv420p", 1080, 1920) and video_facts(path)[-1] == 90
    assert qa.check_segment(path, 90) == []  # the slow zoom means the picture is never frozen


def test_generated_card_renders_cleanly(tmp_path):
    card = GeneratedProvider().card(0, "40%", "household savings")
    info = analyse_source(Selection(0, card, None, None, None, "test"), 3.0)
    path, facts = render_segment(info, scene(3.0), "16:9", [], tmp_path, 90, burn_captions=True)
    assert facts["framing"] == "card"
    assert video_facts(path)[-1] == 90
    assert qa.check_segment(path, 90) == []
    assert "40%" in (tmp_path / "card_000.ass").read_text(encoding="utf-8")


def test_segments_are_cached(tmp_path):
    clip = make_video(tmp_path / "clip.mp4", 3.0)
    info = analyse_source(selection_for(clip), 2.0)
    first, facts1 = render_segment(info, scene(2.0), "1:1", [], tmp_path, 60, burn_captions=False)
    second, facts2 = render_segment(info, scene(2.0), "1:1", [], tmp_path, 60, burn_captions=False)
    assert first == second and facts2.get("cached") is True and "cached" not in facts1


# ---------- Subject-aware crop (a stand-in scorer instead of CLIP) ----------

class SubjectPixelScorer:
    """Stands in for CLIP: a picture's score = how many "subject" (colourful, non-grey) pixels it
    shows. So a window that cuts the subject away scores lower than the whole frame."""
    name = "subject-pixels"
    needs_images = True

    def __init__(self):
        self.calls = 0

    def score_images(self, images, texts):
        self.calls += 1
        out = []
        for img in images:
            grey = np.median(img.reshape(-1, 3), axis=0)
            out.append(float((np.abs(img.astype(int) - grey).max(axis=2) > 40).sum()) / 1e5)
        return out


def test_pick_crop_rule():
    assert pick_crop({"salient": 0.30, "centre": 0.25}, 0.29, 0.02) == "salient"
    assert pick_crop({"left": 0.24, "right": 0.27}, 0.28, 0.02) == "right"   # 0.01 below the frame: still crop
    assert pick_crop({"left": 0.20, "right": 0.21}, 0.28, 0.02) == "blur"    # every window loses the subject
    assert pick_crop({"salient": 0.25, "right": 0.25}, 0.25, 0.02) == "salient"  # tie: first listed wins
    # Real run: the centred hand-with-coins scored 0.223, an edge window 0.247 (CLIP noise) -> keep salient.
    assert pick_crop({"salient": 0.2231, "left": 0.2468, "right": 0.2439}, 0.2296, 0.02, tie_margin=0.03) == "salient"
    # A subject really at the edge wins clearly (real run: 0.236 vs 0.181 the other way round).
    assert pick_crop({"centre": 0.181, "right": 0.236}, 0.232, 0.02, tie_margin=0.03) == "right"


def test_9x16_subject_aware_crop_keeps_the_subject_at_the_edge(tmp_path):
    clip = make_subject_on_right(tmp_path / "subject.mp4", 3.0)
    scorer = SubjectPixelScorer()
    info = analyse_source(selection_for(clip), 2.0, ["a busy test pattern"], scorer)
    framing = plan_framing(info, 1080, 1920)
    assert framing.mode == "subject" and framing.x > 0.5          # window moved right, onto the subject
    crop = framing.crop
    assert crop["method"] == "clip" and crop["choice"] in ("salient", "right")
    assert crop["windows"][crop["choice"]]["score"] > crop["windows"]["centre"]["score"]
    assert crop["frames"] == get_settings().CROP_SAMPLE_FRAMES and f"window '{crop['choice']}' chosen" in crop["reason"]
    # The choice is written into the segment facts (-> timeline.json), also for a cached segment.
    path, facts = render_segment(info, scene(2.0), "9:16", [], tmp_path, 60, burn_captions=False)
    assert facts["crop"] == crop and facts["framing"].startswith("subject crop")
    _, again = render_segment(info, scene(2.0), "9:16", [], tmp_path, 60, burn_captions=False)
    assert again["cached"] is True and again["crop"] == crop


def test_crop_falls_back_to_blurred_fit_when_no_window_keeps_the_subject(tmp_path):
    clip = make_subjects_at_both_edges(tmp_path / "edges.mp4", 3.0)
    info = analyse_source(selection_for(clip), 2.0, ["two busy test patterns"], SubjectPixelScorer())
    framing = plan_framing(info, 1080, 1920)
    assert framing.mode == "blur"
    assert framing.crop["choice"] == "blurred fit" and "every window cuts the subject" in framing.crop["reason"]
    best = max(w["score"] for w in framing.crop["windows"].values())
    assert best < framing.crop["full_frame_score"] - get_settings().CROP_BLUR_MARGIN


def test_without_a_picture_scorer_the_saliency_crop_is_used_and_recorded(tmp_path):
    clip = make_subject_on_right(tmp_path / "subject.mp4", 3.0)
    info = analyse_source(selection_for(clip), 2.0, ["a busy test pattern"], scorer=None)
    framing = plan_framing(info, 1080, 1920)
    assert framing.mode == "salient" and framing.crop["method"] == "saliency"
    assert plan_framing(info, 1920, 1080).crop == {"method": "none", "choice": "fit (same shape)"}
