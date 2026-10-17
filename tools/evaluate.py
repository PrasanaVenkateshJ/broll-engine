"""Turn the test pack evidence into testpack/metrics.md. Every number is computed from files; a number
without evidence is written as "not measured" (never guessed).

Reads:
- testpack/ratings/ratings.csv   my 1-5 ratings (exported from testpack/ratings/rate.html)
- testpack/outputs/run1/          timeline.json + job.json of every input, summary.csv (tools/batch_run.py)
- testpack/expected.json          the outcome each input SHOULD have (written before the run)
- testpack/outputs/perf/          cold / warm 60 s narration jobs (summary.csv + timeline.json)
- testpack/outputs/concurrency/results.json   1, 2, 3 jobs at once (tools/load_test.py)
- testpack/logs/memory_*.csv, testpack/logs/peaks_*.txt   memory samples / exact process peaks
- testpack/notes/hardware.md, testpack/notes/cost_modal.md

Quality metrics (from my ratings):
- average relevance = mean rating; % of rated scenes rated 4 or 5;
- coverage = narration seconds of scenes rated >= 3 / narration seconds of all rated scenes;
- Spearman rank correlation between the CLIP score (relevance_score) and my rating (stock clips only:
  generated cards have no CLIP score).
Pipeline metrics (from timelines, no ratings needed): repeat-clip rate (a clip id used again in the SAME
video), render success rate, footage mix.

    .venv\\Scripts\\python tools\\evaluate.py
"""
import argparse
import csv
import json
import re
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TP = ROOT / "testpack"
NM = "not measured"
LANG_NAMES = {"en": "English", "hi": "Hindi", "ta": "Tamil"}


# ---------- Small helpers ----------

def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def shown(path: Path) -> str:
    """A path as written in the report: relative to the project if it is inside it."""
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def fmt(value, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return NM
    if isinstance(value, float):
        return f"{value:.{digits}f}{suffix}"
    return f"{value}{suffix}"


def pct(part: float | None, whole: float | None) -> str:
    if part is None or not whole:
        return NM
    return f"{100 * part / whole:.1f}%"


def ranks(values: list[float]) -> list[float]:
    """Average ranks (ties share the mean of their positions), 1-based."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return out


def spearman(x: list[float], y: list[float]) -> float | None:
    """Spearman's rho = Pearson correlation of the ranks. None if fewer than 3 pairs or no variation."""
    if len(x) < 3:
        return None
    rx, ry = ranks(x), ranks(y)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    if sxx == 0 or syy == 0:
        return None
    return sxy / (sxx * syy) ** 0.5


# ---------- Loading the evidence ----------

def load_run(run: Path) -> dict:
    """{input name: {"job": job.json, "timeline": timeline.json or None, "summary": summary row}}"""
    rows = {}
    if (run / "summary.csv").is_file():
        with (run / "summary.csv").open(encoding="utf-8") as f:
            rows = {r["input"]: r for r in csv.DictReader(f)}
    out = {}
    for folder in sorted(p for p in run.iterdir() if p.is_dir()) if run.is_dir() else []:
        out[folder.name] = {"job": read_json(folder / "job.json") or {}, "timeline": read_json(folder / "timeline.json"),
                            "manifest": read_json(folder / "manifest.json"), "summary": rows.get(folder.name, {})}
    return out


def load_ratings(path: Path) -> dict:
    """{(job, scene): rating} for rows with a 1-5 rating."""
    if not path.is_file():
        return {}
    out = {}
    with path.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            try:
                rating = int(float(row.get("my_rating") or 0))
            except ValueError:
                continue
            if 1 <= rating <= 5:
                out[(row["job"], int(row["scene"]))] = rating
    return out


def status_of(entry: dict) -> str:
    job = entry["job"]
    if job.get("status") == "succeeded":
        return "succeeded"
    error = job.get("error") or {}
    return error.get("code") or entry["summary"].get("error_code") or job.get("status") or "unknown"


# ---------- Sections ----------

def scene_rows(run_name: str, run: dict, ratings: dict) -> list[dict]:
    rows = []
    for name, entry in run.items():
        tl = entry["timeline"]
        if status_of(entry) != "succeeded" or not tl:
            continue
        for sc in tl["scenes"]:
            rows.append({"job": f"{run_name}/{name}", "language": tl.get("language") or "?", "scene": sc["index"],
                         "seconds": sc["duration"], "score": sc.get("relevance_score"),
                         "rating": ratings.get((f"{run_name}/{name}", sc["index"])), "provider": sc.get("provider"),
                         "clip": f"{sc.get('provider')}:{sc.get('clip_id')}", "generated": bool(sc.get("generated"))})
    return rows


def quality(rows: list[dict]) -> dict:
    rated = [r for r in rows if r["rating"]]
    seconds_rated = sum(r["seconds"] for r in rated)
    pairs = [(r["score"], r["rating"]) for r in rated if r["score"] is not None]
    return {
        "scenes": len(rows), "rated": len(rated),
        "seconds_total": sum(r["seconds"] for r in rows), "seconds_rated": seconds_rated,
        "avg": statistics.fmean(r["rating"] for r in rated) if rated else None,
        "top": sum(1 for r in rated if r["rating"] >= 4), "ok_seconds": sum(r["seconds"] for r in rated if r["rating"] >= 3),
        "spearman": spearman([p[0] for p in pairs], [p[1] for p in pairs]), "spearman_n": len(pairs),
        "hist": {k: sum(1 for r in rated if r["rating"] == k) for k in range(1, 6)},
    }


def repeat_rate(run: dict) -> tuple[int, int, int]:
    """(repeated scenes, footage scenes, clips reused ACROSS videos) - generated cards are not footage."""
    repeated = total = 0
    seen_anywhere: dict[str, set] = defaultdict(set)
    for name, entry in run.items():
        tl = entry["timeline"]
        if status_of(entry) != "succeeded" or not tl:
            continue
        seen = set()
        for sc in tl["scenes"]:
            if sc.get("generated"):
                continue
            clip = f"{sc.get('provider')}:{sc.get('clip_id')}"
            total += 1
            repeated += clip in seen
            seen.add(clip)
            seen_anywhere[clip].add(name)
    return repeated, total, sum(1 for videos in seen_anywhere.values() if len(videos) > 1)


def memory_evidence(logs: Path) -> dict:
    peaks = {"api_mb": None, "worker_mb": None, "ffmpeg_mb": None, "total_mb": None}
    files = sorted(logs.glob("memory_*.csv")) if logs.is_dir() else []
    for f in files:
        with f.open(encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                for k in peaks:
                    try:
                        v = float(row.get(k) or 0)
                    except ValueError:
                        continue
                    peaks[k] = v if peaks[k] is None else max(peaks[k], v)
    exact = {"api": None, "worker": None}
    for f in sorted(logs.glob("peaks_*.txt")) if logs.is_dir() else []:
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"(api|worker),.*peak_mb=([\d.]+)", line.strip())
            if m:
                role, value = m.group(1), float(m.group(2))
                exact[role] = value if exact[role] is None else max(exact[role], value)
    return {"sampled": peaks, "exact": exact, "files": [f.name for f in files]}


def modal_price_per_second(notes: Path) -> float | None:
    if not notes.is_file():
        return None
    m = re.search(r"Total\s*=\s*\$([0-9.]+)\s*/\s*s", notes.read_text(encoding="utf-8"))
    return float(m.group(1)) if m else None


def perf_job(folder: Path) -> dict | None:
    """The one job of a perf batch folder: turnaround + narration + stage timings."""
    if not (folder / "summary.csv").is_file():
        return None
    with (folder / "summary.csv").open(encoding="utf-8") as f:
        row = next(csv.DictReader(f), None)
    if not row or row["status"] != "succeeded":
        return {"status": row["status"] if row else "missing"}
    manifest = read_json(folder / row["input"] / "manifest.json") or {}
    return {"status": "succeeded", "turnaround": float(row["turnaround_seconds"]),
            "narration": float(row["narration_seconds"]), "timings": manifest.get("timings_seconds") or {}}


# ---------- The report ----------

def build_report(args) -> str:
    ratings = load_ratings(args.ratings)
    expected = {k: v for k, v in (read_json(args.expected) or {}).items() if not k.startswith("_")}
    runs = {run.name: load_run(run) for run in args.runs}
    main_name, main_run = next(iter(runs.items()))
    rows = [r for name, run in runs.items() for r in scene_rows(name, run, ratings)]
    q = quality(rows)
    rho_n = f" (n = {q['spearman_n']})" if q["spearman"] is not None else ""
    out = ["# Metrics", "",
           f"Generated by `tools/evaluate.py` on {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} from the "
           "files in `testpack/` (nothing typed in by hand). \"not measured\" = no evidence file for that number.", ""]

    # 1. Relevance (my ratings)
    out += ["## 1. Relevance (my ratings)", "",
            f"Ratings file: `{shown(args.ratings)}` "
            f"({'found' if args.ratings.is_file() else 'NOT FOUND - rate the scenes in testpack/ratings/rate.html'}).", "",
            "| Metric | Value |", "|---|---|",
            f"| Scenes in successful videos | {q['scenes']} |",
            f"| Scenes rated | {q['rated']} ({pct(q['rated'], q['scenes'])}) |",
            f"| Average relevance (1-5) | {fmt(q['avg'])} |",
            f"| Scenes rated 4-5 | {pct(q['top'], q['rated']) if q['rated'] else NM} |",
            f"| Coverage: narration time rated >= 3 / rated narration time | "
            f"{pct(q['ok_seconds'], q['seconds_rated']) if q['rated'] else NM} |",
            f"| Spearman correlation CLIP score vs my rating (stock clips) | "
            f"{fmt(q['spearman'], 3)}{rho_n} |",
            f"| Rating counts 1/2/3/4/5 | {' / '.join(str(q['hist'][k]) for k in range(1, 6)) if q['rated'] else NM} |",
            ""]

    # 2. Per language
    out += ["## 2. Per language", "",
            "| Language | Inputs expected to succeed | Succeeded | Scenes | Rated | Average | Rated 4-5 | Coverage >= 3 |",
            "|---|---|---|---|---|---|---|---|"]
    for lang in ("en", "hi", "ta"):
        names = [n for n, e in expected.items() if e.get("language") == lang and e.get("expect") == "succeeded"]
        ok = [n for n in names if n in main_run and status_of(main_run[n]) == "succeeded"]
        lq = quality([r for r in rows if r["language"] == lang])
        out.append(f"| {LANG_NAMES[lang]} ({lang}) | {len(names)} | {len(ok)} | {lq['scenes']} | {lq['rated']} | "
                   f"{fmt(lq['avg'])} | {pct(lq['top'], lq['rated']) if lq['rated'] else NM} | "
                   f"{pct(lq['ok_seconds'], lq['seconds_rated']) if lq['rated'] else NM} |")
    out.append("")

    # 3. Render success + expected outcomes
    out += ["## 3. Render success and expected outcomes", ""]
    valid = [n for n, e in expected.items() if e.get("expect") == "succeeded"]
    valid_ok = [n for n in valid if n in main_run and status_of(main_run[n]) == "succeeded"]
    matched = [n for n, e in expected.items() if n in main_run and status_of(main_run[n]) == e.get("expect")]
    out += [f"- Render success rate (inputs that should make a video): **{len(valid_ok)} / {len(valid)}** "
            f"({pct(len(valid_ok), len(valid))})",
            f"- Every input with the expected outcome (videos AND the right error codes): **{len(matched)} / "
            f"{len(expected)}** ({pct(len(matched), len(expected))})",
            f"- Inputs in `{main_name}` without an expected outcome: "
            f"{', '.join(sorted(set(main_run) - set(expected))) or 'none'}", "",
            "| Input | Expected | Actual | Narration s | Turnaround s | Scenes | Warnings |", "|---|---|---|---|---|---|---|"]
    for name in sorted(set(expected) | set(main_run)):
        entry = main_run.get(name)
        actual = status_of(entry) if entry else "not run"
        mark = "" if expected.get(name, {}).get("expect") == actual else " **(unexpected)**"
        s = (entry or {}).get("summary", {})
        out.append(f"| {name} | {expected.get(name, {}).get('expect', '?')} | {actual}{mark} | "
                   f"{s.get('narration_seconds') or ''} | {s.get('turnaround_seconds') or ''} | {s.get('scenes') or ''} | "
                   f"{s.get('warnings_count') or ''} |")
    out.append("")

    # 4. Pipeline facts from the timelines
    repeated, footage, across = repeat_rate(main_run)
    mix = defaultdict(int)
    for r in [r for r in rows if r["job"].startswith(f"{main_name}/")]:
        mix[r["provider"]] += 1
    total_scenes = sum(mix.values())
    out += ["## 4. Footage facts (from timeline.json, no ratings needed)", "",
            f"- Repeat-clip rate (same clip twice in ONE video): **{pct(repeated, footage) if footage else NM}** "
            f"({repeated} of {footage} stock scenes)",
            f"- Clips that appear in more than one video (allowed, for information): {across}",
            "- Source of each scene: " + (", ".join(f"{k} {v} ({pct(v, total_scenes)})" for k, v in
                                                  sorted(mix.items(), key=lambda kv: -kv[1])) or NM), ""]

    # 5. Production numbers
    out += ["## 5. Production numbers", ""]
    hardware = args.hardware.read_text(encoding="utf-8").strip() if args.hardware.is_file() else None
    out += ["### Hardware", "", hardware or NM, ""]
    out += ["### Turnaround for about 60 s of narration (16:9 + 9:16, one job alone)", "",
            "| Case | Narration s | Turnaround s | Seconds per narration minute | TTS | Transcribe | Footage | Render |",
            "|---|---|---|---|---|---|---|---|"]
    for label, folder in (("Cold (worker just started, empty footage cache)", "cold_60s"),
                          ("Warm, same script again", "warm_same_script"),
                          ("Warm, new script", "warm_new_script")):
        p = perf_job(args.perf / folder)
        if not p or p.get("status") != "succeeded":
            out.append(f"| {label} | {NM} | {NM} | {NM} | | | | |")
            continue
        t = p["timings"]
        out.append(f"| {label} | {p['narration']:.1f} | {p['turnaround']:.1f} | {p['turnaround'] / p['narration'] * 60:.0f} | "
                   f"{t.get('tts', 0):.0f} | {t.get('transcribe', 0):.0f} | {t.get('footage', 0):.0f} | {t.get('render', 0):.0f} |")
    out.append("")

    done = [(name, e) for name, e in main_run.items() if status_of(e) == "succeeded" and e["summary"].get("turnaround_seconds")]
    per_min = [float(e["summary"]["turnaround_seconds"]) / (float(e["summary"]["narration_seconds"]) / 60) for _, e in done]
    video_minutes = sum(float(e["summary"]["narration_seconds"]) / 60 * len((e["timeline"] or {}).get("videos") or [])
                        for _, e in done)
    total_turn = sum(float(e["summary"]["turnaround_seconds"]) for _, e in done)
    out += [f"### Time per minute of finished video (`{main_name}`, {len(done)} succeeded jobs, one at a time)", "",
            f"- Median turnaround per minute of narration: **{fmt(statistics.median(per_min) if per_min else None, 0, ' s')}** "
            f"(min {fmt(min(per_min) if per_min else None, 0, ' s')}, max {fmt(max(per_min) if per_min else None, 0, ' s')})",
            f"- All succeeded jobs together: {fmt(total_turn / 60 if done else None, 1, ' min')} of processing for "
            f"{fmt(video_minutes if done else None, 1, ' min')} of finished video across all aspect ratios = "
            f"{fmt(total_turn / video_minutes if video_minutes else None, 0, ' s')} per minute of output video", ""]

    mem = memory_evidence(args.logs)
    smp, ex = mem["sampled"], mem["exact"]
    out += ["### Peak memory", "",
            "| Process | Exact peak (Windows peak working set) | Highest 1-second sample |", "|---|---|---|",
            f"| API (uvicorn) | {fmt(ex['api'], 0, ' MB')} | {fmt(smp['api_mb'], 0, ' MB')} |",
            f"| Worker (Python: Kokoro + Whisper + CLIP) | {fmt(ex['worker'], 0, ' MB')} | {fmt(smp['worker_mb'], 0, ' MB')} |",
            f"| ffmpeg child processes (sum) | - | {fmt(smp['ffmpeg_mb'], 0, ' MB')} |",
            f"| API + worker + ffmpeg (same moment) | - | {fmt(smp['total_mb'], 0, ' MB')} |", "",
            f"Sample files: {', '.join(mem['files']) or NM}.", ""]

    conc = read_json(args.concurrency)
    out += ["### Jobs at the same time on this machine", ""]
    if conc and conc.get("levels"):
        out += ["| Jobs at once | Succeeded | Wall s | Narration min done | Throughput (narration min per wall min) | "
                "Jobs per hour | Mean turnaround s | Peak memory total MB (worker MB) |", "|---|---|---|---|---|---|---|---|"]
        for lv in conc["levels"]:
            turns = [j["turnaround_seconds"] for j in lv["jobs"] if j["turnaround_seconds"]]
            m = lv.get("memory_peak_mb") or {}
            out.append(f"| {lv['level']} | {lv['succeeded']}/{lv['level']} | {lv['wall_seconds']:.0f} | "
                       f"{lv['narration_minutes_finished']:.2f} | {fmt(lv['throughput_narration_min_per_wall_min'], 3)} | "
                       f"{fmt(lv['jobs_per_hour'], 1)} | {fmt(statistics.fmean(turns) if turns else None, 0)} | "
                       f"{fmt(m.get('total_mb'), 0)} ({fmt(m.get('worker_mb'), 0)}) |")
    else:
        out.append(NM)
    out.append("")

    price = modal_price_per_second(args.cost_notes)
    median_min = statistics.median(per_min) if per_min else None
    tokens = [((e["manifest"] or {}).get("models", {}).get("understand", {}).get("tokens_used") or 0,
               float(e["summary"]["narration_seconds"])) for _, e in done]
    tokens_per_min = (sum(t for t, _ in tokens) / (sum(s for _, s in tokens) / 60)) if tokens else None
    out += ["### Cost per minute", "",
            "- Local (this PC): **$0** - Kokoro, Whisper, open_clip and FFmpeg run on the CPU; Groq (free tier) and the "
            f"Pixabay / Wikimedia APIs are free. Groq tokens used: {fmt(tokens_per_min, 0)} per narration minute "
            "(free tier, so $0 here).",
            f"- Modal estimate: container price {fmt(price, 8)} $/s (2 cores + 3 GiB, from `testpack/notes/cost_modal.md`) "
            f"x the median {fmt(median_min, 0)} s per narration minute measured HERE = "
            f"**{fmt(price * median_min if price and median_min else None, 4)} $ per narration minute**, plus a cold start "
            "+ 180 s idle tail per visit (see the notes). ESTIMATE: Modal run times were not measured; a 2-core "
            "Modal container may be slower than this 16-thread PC.", ""]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write testpack/metrics.md from the test pack evidence.")
    parser.add_argument("--ratings", type=Path, default=TP / "ratings" / "ratings.csv")
    parser.add_argument("--runs", type=Path, nargs="+", default=[TP / "outputs" / "run1"])
    parser.add_argument("--expected", type=Path, default=TP / "expected.json")
    parser.add_argument("--perf", type=Path, default=TP / "outputs" / "perf")
    parser.add_argument("--concurrency", type=Path, default=TP / "outputs" / "concurrency" / "results.json")
    parser.add_argument("--logs", type=Path, default=TP / "logs")
    parser.add_argument("--hardware", type=Path, default=TP / "notes" / "hardware.md")
    parser.add_argument("--cost-notes", type=Path, default=TP / "notes" / "cost_modal.md")
    parser.add_argument("--out", type=Path, default=TP / "metrics.md")
    args = parser.parse_args(argv)
    args.ratings, args.out = args.ratings.resolve(), args.out.resolve()
    args.runs = [r.resolve() for r in args.runs]
    report = build_report(args)
    args.out.write_text(report, encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
