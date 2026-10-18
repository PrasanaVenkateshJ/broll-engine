"""Compare two test pack runs (e.g. run1 vs run2) from their files only, and write a markdown report.

Per run (succeeded jobs, every scene): share of scenes on real stock VIDEO / photo-motion / generated cards
(number cards and keyword cards counted apart), number badges on footage, adaptive-threshold clips, the
average CLIP relevance score of stock clips, the independent judge's ratings, how many scenes Groq could
not answer (rules fallback) and the 60 s turnaround from the perf folders.

Note: run 2 scores differently (prompt ensembling, mean of the best 3 of 5 queries, extra frames), so the
average CLIP score is NOT on exactly the same scale in both runs; the report says so.

    .venv\\Scripts\\python tools\\compare_runs.py --before testpack/outputs/run1 --after testpack/outputs/run2 \\
        --before-perf testpack/outputs/perf --after-perf testpack/outputs/perf_run2 --out testpack/compare_run1_run2.md
"""
import argparse
import csv
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PERF_CASES = (("cold_60s", "Cold (worker just started, empty caches)"),
              ("warm_same_script", "Warm, same script again"),
              ("warm_new_script", "Warm, new script"))
STAGES = ("tts", "transcribe", "understand", "footage", "render")


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def jobs(run: Path) -> list[dict]:
    """[{name, job, timeline, manifest}] for every succeeded job folder of a run."""
    out = []
    for folder in sorted(p for p in run.iterdir() if p.is_dir()) if run.is_dir() else []:
        job, timeline = read_json(folder / "job.json"), read_json(folder / "timeline.json")
        if job and job.get("status") == "succeeded" and timeline:
            out.append({"name": folder.name, "job": job, "timeline": timeline,
                        "manifest": read_json(folder / "manifest.json") or {}})
    return out


def kind_of(scene: dict) -> str:
    if scene.get("generated"):
        return "number_card" if (scene.get("reason") or "").startswith("statistic line") else "keyword_card"
    return "photo_motion" if (scene.get("clip_details") or {}).get("kind") == "image" else "video"


def summarise(run: Path) -> dict:
    found = jobs(run)
    scenes = [sc for j in found for sc in j["timeline"]["scenes"]]
    counts = {k: 0 for k in ("video", "photo_motion", "number_card", "keyword_card")}
    for sc in scenes:
        counts[kind_of(sc)] += 1
    stock = [sc["relevance_score"] for sc in scenes if not sc.get("generated") and sc.get("relevance_score") is not None]
    judged = [sc["vlm_score"] for sc in scenes if sc.get("vlm_score") is not None]
    rules_scenes, fallback_jobs = 0, 0
    for j in found:
        und = (j["manifest"].get("models") or {}).get("understand") or {}
        if und.get("provider") == "groq":
            fell = [sc for sc in j["timeline"]["scenes"] if (sc.get("understanding") or {}).get("source") == "rules"]
            rules_scenes += len(fell)
            fallback_jobs += bool(fell)
    n = len(scenes)
    return {
        "jobs": len(found), "scenes": n, "counts": counts,
        "share": {k: (v / n if n else None) for k, v in counts.items()},
        "overlays": sum(1 for sc in scenes if sc.get("overlay")),
        "adaptive": sum(1 for sc in scenes if (sc.get("score_details") or {}).get("adaptive")),
        "story_sensitive_scenes": sum(1 for sc in scenes if sc.get("story_sensitive")),
        "clip_mean": statistics.mean(stock) if stock else None, "clip_median": statistics.median(stock) if stock else None,
        "stock_scenes": len(stock), "vlm_judged": len(judged), "vlm_mean": statistics.mean(judged) if judged else None,
        "vlm_counts": {r: judged.count(r) for r in range(1, 6)} if judged else {},
        "groq_rules_scenes": rules_scenes, "groq_fallback_jobs": fallback_jobs,
        "per_job": {j["name"]: j for j in found},
    }


def perf(folder: Path) -> dict:
    """{case: {narration, turnaround, stages}} from tools/batch_run.py output folders."""
    out = {}
    for case, _ in PERF_CASES:
        rows = list(csv.DictReader((folder / case / "summary.csv").open(encoding="utf-8"))) \
            if (folder / case / "summary.csv").is_file() else []
        row = next((r for r in rows if r.get("status") == "succeeded"), None)
        if not row:
            continue
        manifest = read_json(folder / case / row["input"] / "manifest.json") or {}
        out[case] = {"narration": float(row["narration_seconds"]), "turnaround": float(row["turnaround_seconds"]),
                     "stages": manifest.get("timings_seconds") or {}}
    return out


def pct(x):
    return "-" if x is None else f"{100 * x:.1f}%"


def num(x, d=3):
    return "-" if x is None else f"{x:.{d}f}"


def report(before: dict, after: dict, perf_before: dict, perf_after: dict, names: tuple[str, str]) -> str:
    b, a = names
    lines = [f"# {b} vs {a}: footage quality and speed", "",
             "Written by `tools/compare_runs.py` from the run folders (nothing typed in by hand).", "",
             f"| Metric | {b} | {a} |", "|---|---|---|",
             f"| Succeeded jobs / scenes | {before['jobs']} / {before['scenes']} | {after['jobs']} / {after['scenes']} |"]
    for key, label in (("video", "Scenes on real stock VIDEO"), ("photo_motion", "Scenes on photo-motion"),
                       ("number_card", "Plain number cards (statistics)"), ("keyword_card", "Plain keyword cards")):
        lines.append(f"| {label} | {before['counts'][key]} ({pct(before['share'][key])}) | "
                     f"{after['counts'][key]} ({pct(after['share'][key])}) |")
    lines += [
        f"| Statistic numbers drawn as a badge on real footage | {before['overlays']} | {after['overlays']} |",
        f"| Clips accepted by the adaptive threshold (judge agreed) | {before['adaptive']} | {after['adaptive']} |",
        f"| Average CLIP score of stock clips (median) * | {num(before['clip_mean'])} ({num(before['clip_median'])}) | "
        f"{num(after['clip_mean'])} ({num(after['clip_median'])}) |",
        f"| Clips rated by the VLM judge / mean rating 1-5 | {before['vlm_judged']} / {num(before['vlm_mean'], 2)} | "
        f"{after['vlm_judged']} / {num(after['vlm_mean'], 2)} |",
        f"| Judge ratings 1/2/3/4/5 | {fmt_counts(before['vlm_counts'])} | {fmt_counts(after['vlm_counts'])} |",
        f"| Scenes Groq could not answer (offline rules used) / jobs affected | {before['groq_rules_scenes']} / "
        f"{before['groq_fallback_jobs']} | {after['groq_rules_scenes']} / {after['groq_fallback_jobs']} |",
        f"| Scenes with the calm rules of a sensitive STORY | {before['story_sensitive_scenes']} | "
        f"{after['story_sensitive_scenes']} |",
        "",
        f"\\* {a} scores differently (prompt ensembling, mean of the best 3 of 5 queries, extra frames for the best "
        "videos), so the average CLIP score is not on exactly the same scale; compare the shares and the "
        "ratings instead.", "",
        "## Turnaround for about 60 s of narration (16:9 + 9:16, one job alone)", "",
        f"| Case | {b} narration s | {b} turnaround s | {a} narration s | {a} turnaround s | {a} vs {b} |",
        "|---|---|---|---|---|---|"]
    for case, label in PERF_CASES:
        pb, pa = perf_before.get(case), perf_after.get(case)
        change = f"{100 * (pa['turnaround'] / pb['turnaround'] - 1):+.0f}%" if pb and pa else "-"
        lines.append(f"| {label} | {num(pb and pb['narration'], 1)} | {num(pb and pb['turnaround'], 1)} | "
                     f"{num(pa and pa['narration'], 1)} | {num(pa and pa['turnaround'], 1)} | {change} |")
    lines += ["", "Seconds per stage (from each job's manifest.json `timings_seconds`):", "",
              "| Case | Run | " + " | ".join(STAGES) + " |", "|---|---|" + "---|" * len(STAGES)]
    for case, label in PERF_CASES:
        for name, data in ((b, perf_before.get(case)), (a, perf_after.get(case))):
            if data:
                lines.append(f"| {label} | {name} | " + " | ".join(num(data["stages"].get(s), 1) for s in STAGES) + " |")
    lines += ["", "## Per input", "", f"| Input | {b} video/photo/number card/keyword card | {a} video/photo/number "
              f"card/keyword card | {a} badges | {b} turnaround s | {a} turnaround s |", "|---|---|---|---|---|---|"]
    for name in sorted(set(before["per_job"]) | set(after["per_job"])):
        cells = []
        for run in (before, after):
            job = run["per_job"].get(name)
            if job:
                c = [kind_of(sc) for sc in job["timeline"]["scenes"]]
                cells.append("/".join(str(c.count(k)) for k in ("video", "photo_motion", "number_card", "keyword_card")))
            else:
                cells.append("-")
        badges = sum(1 for sc in after["per_job"][name]["timeline"]["scenes"] if sc.get("overlay")) \
            if name in after["per_job"] else "-"
        times = [num(_turnaround(run["per_job"].get(name)), 1) for run in (before, after)]
        lines.append(f"| {name} | {cells[0]} | {cells[1]} | {badges} | {times[0]} | {times[1]} |")
    return "\n".join(lines) + "\n"


def _turnaround(job: dict | None):
    if not job:
        return None
    from datetime import datetime
    j = job["job"]
    try:
        return (datetime.fromisoformat(j["finished_at"]) - datetime.fromisoformat(j["created_at"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        return None


def job_facts(folder: Path) -> dict | None:
    """One job folder -> turnaround, candidates scored, scene kinds, badges, Groq fallback scenes, mode."""
    job, timeline = read_json(folder / "job.json"), read_json(folder / "timeline.json")
    if not job or job.get("status") != "succeeded" or not timeline:
        return None
    manifest = read_json(folder / "manifest.json") or {}
    models = manifest.get("models") or {}
    kinds = [kind_of(sc) for sc in timeline["scenes"]]
    judged = [sc["vlm_score"] for sc in timeline["scenes"] if sc.get("vlm_score") is not None]
    return {"turnaround": _turnaround({"job": job}), "candidates": (models.get("ranker") or {}).get("candidates_scored"),
            "kinds": {k: kinds.count(k) for k in ("video", "photo_motion", "number_card", "keyword_card")},
            "badges": sum(1 for sc in timeline["scenes"] if sc.get("overlay")),
            "rules_scenes": len((models.get("understand") or {}).get("fallback_scenes") or []),
            "vlm": f"{len(judged)} / {statistics.mean(judged):.2f}" if judged else "0 / -",
            "mode": manifest.get("quality_mode") or "-", "footage": (manifest.get("timings_seconds") or {}).get("footage")}


def pairs_table(pairs: list[tuple[str, Path, Path]], names: tuple[str, str]) -> str:
    """Before/after for chosen job folders (e.g. the run-1 and run-2 job of the same input)."""
    b, a = names
    lines = [f"| Input | Turnaround s {b} -> {a} | Footage stage s | Candidates scored | Plain cards (number + keyword) | "
             f"video / photo-motion | Number badges | Judge rated / mean | Groq fallback scenes | Mode |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for label, before_dir, after_dir in pairs:
        x, y = job_facts(before_dir), job_facts(after_dir)
        if not x or not y:
            lines.append(f"| {label} | missing: {before_dir if not x else after_dir} |" + " |" * 8)
            continue
        cards = [f["kinds"]["number_card"] + f["kinds"]["keyword_card"] for f in (x, y)]
        cand = x["candidates"] if x["candidates"] is not None else "not recorded"
        lines.append(f"| {label} | {num(x['turnaround'], 1)} -> {num(y['turnaround'], 1)} | {num(x['footage'], 1)} -> "
                     f"{num(y['footage'], 1)} | {cand} -> {y['candidates']} | {cards[0]} -> {cards[1]} | "
                     f"{x['kinds']['video']}/{x['kinds']['photo_motion']} -> {y['kinds']['video']}/{y['kinds']['photo_motion']} | "
                     f"{x['badges']} -> {y['badges']} | {x['vlm']} -> {y['vlm']} | {x['rules_scenes']} -> {y['rules_scenes']} | "
                     f"{y['mode']} |")
    return "\n".join(lines) + "\n"


def fmt_counts(counts: dict) -> str:
    return "/".join(str(counts.get(r, 0)) for r in range(1, 6)) if counts else "-"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare two test pack runs.")
    parser.add_argument("--before", type=Path, default=ROOT / "testpack" / "outputs" / "run1")
    parser.add_argument("--after", type=Path, default=ROOT / "testpack" / "outputs" / "run2")
    parser.add_argument("--before-perf", type=Path, default=ROOT / "testpack" / "outputs" / "perf")
    parser.add_argument("--after-perf", type=Path, default=ROOT / "testpack" / "outputs" / "perf_run2")
    parser.add_argument("--out", type=Path, default=ROOT / "testpack" / "compare_run1_run2.md")
    parser.add_argument("--pair", action="append", default=[], metavar="LABEL=BEFORE_DIR,AFTER_DIR",
                        help="compare these two job folders only (repeatable); writes just that table")
    args = parser.parse_args(argv)
    if args.pair:
        pairs = []
        for item in args.pair:
            label, _, dirs = item.partition("=")
            before_dir, _, after_dir = dirs.partition(",")
            pairs.append((label, Path(before_dir), Path(after_dir)))
        args.out.write_text(pairs_table(pairs, ("run1", "run2")), encoding="utf-8", newline="\n")
        print(f"wrote {args.out}")
        return 0
    text = report(summarise(args.before), summarise(args.after), perf(args.before_perf), perf(args.after_perf),
                  (args.before.name, args.after.name))
    args.out.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
