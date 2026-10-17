"""tools/make_rating_page.py and tools/evaluate.py on a tiny fake test pack."""
import json
from types import SimpleNamespace

import pytest

from tools import evaluate
from tools.make_rating_page import collect_scenes, render_page


def fake_run(root, name="core01", status="succeeded", scenes=None):
    folder = root / "run1" / name
    folder.mkdir(parents=True)
    (folder / "job.json").write_text(json.dumps({"status": status}), encoding="utf-8")
    scenes = scenes or [
        {"index": 0, "start": 0.0, "end": 4.0, "duration": 4.0, "text": "Save money </script> now.",
         "provider": "pixabay_video", "clip_id": "1", "relevance_score": 0.30, "generated": False, "thumbnail": None},
        {"index": 1, "start": 4.0, "end": 10.0, "duration": 6.0, "text": "Sales grew 40%.",
         "provider": "generated", "clip_id": "card-1", "relevance_score": None, "generated": True, "thumbnail": None},
    ]
    (folder / "timeline.json").write_text(json.dumps({"job_id": "j1", "language": "en", "duration": 10.0,
                                                      "scenes": scenes}), encoding="utf-8")
    (folder / "video_16x9.mp4").write_bytes(b"x")
    return folder


def test_rating_page_lists_every_scene_and_cannot_be_broken_by_the_text(tmp_path):
    fake_run(tmp_path)
    fake_run(tmp_path, "failed_one", status="failed")
    jobs, scenes = collect_scenes([tmp_path / "run1"], tmp_path / "ratings")
    assert [j["job"] for j in jobs] == ["run1/core01"]
    assert [(s["job"], s["scene"]) for s in scenes] == [("run1/core01", 0), ("run1/core01", 1)]
    assert scenes[0]["video"] == "../run1/core01/video_16x9.mp4"
    page = render_page(jobs, scenes, "t")
    assert page.count("</script>") == 2  # only the page's own two script tags close
    assert "Export CSV" in page and "localStorage" in page


def test_spearman_with_ties_and_too_little_data():
    assert evaluate.spearman([0.1, 0.2, 0.3, 0.4], [1, 2, 3, 4]) == pytest.approx(1.0)
    assert evaluate.spearman([0.1, 0.2, 0.3, 0.4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert evaluate.ranks([5, 1, 5, 3]) == [3.5, 1.0, 3.5, 2.0]
    assert evaluate.spearman([0.1, 0.2], [1, 2]) is None
    assert evaluate.spearman([0.1, 0.2, 0.3], [3, 3, 3]) is None


def test_quality_coverage_counts_narration_time():
    rows = [{"seconds": 4.0, "rating": 5, "score": 0.3}, {"seconds": 6.0, "rating": 2, "score": 0.2},
            {"seconds": 5.0, "rating": None, "score": 0.25}]
    q = evaluate.quality(rows)
    assert q["rated"] == 2 and q["avg"] == 3.5 and q["top"] == 1
    assert q["ok_seconds"] / q["seconds_rated"] == pytest.approx(0.4)


def test_report_says_not_measured_without_evidence(tmp_path):
    fake_run(tmp_path)
    args = SimpleNamespace(ratings=tmp_path / "none.csv", expected=tmp_path / "none.json", runs=[tmp_path / "run1"],
                           perf=tmp_path / "perf", concurrency=tmp_path / "c.json", logs=tmp_path / "logs",
                           hardware=tmp_path / "hw.md", cost_notes=tmp_path / "cost.md")
    report = evaluate.build_report(args)
    assert "| Average relevance (1-5) | not measured |" in report
    assert "Repeat-clip rate (same clip twice in ONE video): **0.0%**" in report
