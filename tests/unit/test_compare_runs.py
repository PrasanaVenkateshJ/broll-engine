"""tools/compare_runs.py: shares, badges, judge ratings and Groq fallbacks are counted from the files."""
import json

from tools import compare_runs


def write_job(folder, scenes, provider="groq"):
    folder.mkdir(parents=True)
    (folder / "job.json").write_text(json.dumps({"status": "succeeded", "created_at": "2026-10-17T10:00:00+00:00",
                                                 "finished_at": "2026-10-17T10:01:40+00:00"}), encoding="utf-8")
    (folder / "timeline.json").write_text(json.dumps({"scenes": scenes}), encoding="utf-8")
    (folder / "manifest.json").write_text(json.dumps({"models": {"understand": {"provider": provider}}}),
                                          encoding="utf-8")


def scene(kind, score=None, reason="x", overlay=False, vlm=None, source="groq"):
    return {"generated": kind == "card", "reason": reason, "relevance_score": score, "overlay": overlay,
            "vlm_score": vlm, "clip_details": {"kind": "image" if kind == "photo" else "video"},
            "understanding": {"source": source}}


def test_compare_counts_everything_from_the_run_folders(tmp_path):
    write_job(tmp_path / "run1" / "a", [scene("video", 0.25), scene("card", reason="statistic line: number card"),
                                        scene("card", source="rules")])
    write_job(tmp_path / "run2" / "a", [scene("video", 0.27, vlm=5), scene("video", 0.24, overlay=True),
                                        scene("photo", 0.23, vlm=3)])
    before, after = compare_runs.summarise(tmp_path / "run1"), compare_runs.summarise(tmp_path / "run2")
    assert before["counts"] == {"video": 1, "photo_motion": 0, "number_card": 1, "keyword_card": 1}
    assert after["counts"] == {"video": 2, "photo_motion": 1, "number_card": 0, "keyword_card": 0}
    assert after["overlays"] == 1 and after["vlm_mean"] == 4 and before["groq_rules_scenes"] == 1
    text = compare_runs.report(before, after, {}, {}, ("run1", "run2"))
    assert "| Scenes on real stock VIDEO | 1 (33.3%) | 2 (66.7%) |" in text
    assert "| a | 1/0/1/1 | 2/1/0/0 | 1 | 100.0 | 100.0 |" in text


def test_pairs_table_compares_two_job_folders(tmp_path):
    write_job(tmp_path / "a1", [scene("video", 0.25), scene("card", reason="statistic line: number card")])
    write_job(tmp_path / "a2", [scene("video", 0.27, vlm=4), scene("video", 0.24, overlay=True)])
    text = compare_runs.pairs_table([("a", tmp_path / "a1", tmp_path / "a2")], ("run1", "run2"))
    assert "| a | 100.0 -> 100.0 | - -> - | not recorded -> None | 1 -> 0 | 1/0 -> 2/0 | 0 -> 1 | 0 / - -> 1 / 4.00 |" in text
