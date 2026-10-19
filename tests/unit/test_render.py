"""Rendering one segment: exact length, size, fps, pixel format, salient crop, slow/loop, cards."""
from fractions import Fraction

import numpy as np
import pytest

from app.config import get_settings
from app.pipeline import media, qa
from app.pipeline.providers.base import Candidate
from app.pipeline.providers.generated import GeneratedProvider
from app.pipeline.rank import Selection
from app.pipeline.render import (SourceInfo, analyse_source, choose_window, locate_subject, look_filter,
                                 place_window, plan_framing, render_segment, time_plan)
from app.pipeline.scenes import Scene
from app.pipeline.transcribe import Word
from tests.media_helpers import (make_image, make_moving_subject, make_plain_video, make_subject_at,
                                 make_subject_on_right, make_subjects_at_both_edges, make_video)


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


def test_9x16_subject_aware_crop_keeps_the_subject_at_the_edge(tmp_path):
    clip = make_subject_on_right(tmp_path / "subject.mp4", 3.0)
    scorer = SubjectPixelScorer()
    info = analyse_source(selection_for(clip), 2.0, ["a busy test pattern"], scorer)
    framing = plan_framing(info, 1080, 1920)
    assert framing.mode == "subject" and framing.x > 0.5          # window moved right, onto the subject
    crop = framing.crop
    assert crop["method"] == "clip" and crop["choice"] == "subject" and "whole subject inside" in crop["reason"]
    assert crop["frames"] == get_settings().CROP_SAMPLE_FRAMES and crop["strips"] == get_settings().CROP_STRIPS
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
    assert framing.crop["window_score"] < framing.crop["full_frame_score"] - get_settings().CROP_BLUR_MARGIN


def test_without_a_picture_scorer_the_saliency_crop_is_used_and_recorded(tmp_path):
    clip = make_subject_on_right(tmp_path / "subject.mp4", 3.0)
    info = analyse_source(selection_for(clip), 2.0, ["a busy test pattern"], scorer=None)
    framing = plan_framing(info, 1080, 1920)
    assert framing.mode == "salient" and framing.crop["method"] == "saliency"
    assert plan_framing(info, 1920, 1080).crop == {"method": "none", "choice": "fit (same shape)"}


# ---------- Subject at the far edges, moving, or missing (hard case 5) ----------

def subject_inside(framing, left_px, right_px, width=640):
    """Is the subject (pixels left_px..right_px of a 640-wide frame) fully inside the crop window?"""
    return framing.x <= left_px / width + 1e-3 and right_px / width <= framing.x + framing.w + 1e-3


@pytest.mark.parametrize("x", [10, 520])  # far left / far right
def test_clip_crop_keeps_a_subject_at_the_far_edge_whole(tmp_path, x):
    clip = make_subject_at(tmp_path / f"edge_{x}.mp4", x)
    info = analyse_source(selection_for(clip), 2.0, ["a busy test pattern"], SubjectPixelScorer())
    framing = plan_framing(info, 1080, 1920)
    assert framing.mode == "subject" and framing.crop["choice"] == "subject" and framing.end is None
    assert subject_inside(framing, x, x + 110), (framing.x, framing.w)
    assert "whole subject inside" in framing.crop["reason"] and "warning" not in framing.crop


@pytest.mark.parametrize("x", [10, 520])
def test_saliency_crop_keeps_a_subject_at_the_far_edge_whole(tmp_path, x):
    clip = make_subject_at(tmp_path / f"edge_{x}.mp4", x)
    info = analyse_source(selection_for(clip), 2.0, ["a busy test pattern"], scorer=None)
    framing = plan_framing(info, 1080, 1920)
    assert framing.mode == "salient" and subject_inside(framing, x, x + 110), (framing.x, framing.w)


def test_the_crop_follows_a_moving_subject_smoothly(tmp_path):
    clip = make_moving_subject(tmp_path / "moving.mp4", 3.0)
    info = analyse_source(selection_for(clip), 2.0, ["a busy test pattern"], SubjectPixelScorer())
    framing = plan_framing(info, 1080, 1920)
    assert framing.crop["choice"] == "follow" and framing.end - framing.x > 0.3   # pans left -> right with it
    path, facts = render_segment(info, scene(2.0), "9:16", [], tmp_path, 60, burn_captions=False)
    assert video_facts(path)[2:4] == (1080, 1920) and video_facts(path)[-1] == 60
    assert facts["crop"]["choice"] == "follow" and qa.check_segment(path, 60) == []


def test_no_subject_falls_back_to_the_centre_with_a_warning(tmp_path):
    clip = make_plain_video(tmp_path / "plain.mp4", 3.0)
    info = analyse_source(selection_for(clip), 2.0, ["a busy test pattern"], SubjectPixelScorer())
    framing = plan_framing(info, 1080, 1920)
    assert framing.crop["choice"] == "centre" and abs(framing.x - (1 - framing.w) / 2) < 1e-3
    assert "no clear subject found" in framing.crop["warning"]


def test_locate_subject_and_place_window():
    centres = np.linspace(0.0, 0.84, 13) + 0.08
    flat = np.full((2, 13), 0.25)
    assert locate_subject(flat, centres, 0.16, 0.02)["found"] is False
    edge = flat.copy()
    edge[:, -2:] = 0.30                                   # subject at the far right edge
    found = locate_subject(edge, centres, 0.16, 0.02)
    assert found["found"] and found["span"][1] == 1.0
    start = place_window(found["span"], found["centre"], 0.316)
    assert start == round(1 - 0.316, 4) and start <= found["span"][0]   # window flush right, subject inside
    assert place_window((0.0, 0.1), 0.05, 0.316) == 0.0                 # far left: flush left
