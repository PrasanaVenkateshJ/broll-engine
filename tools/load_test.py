"""How many jobs can one machine run at once? Throughput and memory at 1, 2 and 3 concurrent jobs.

For each level N, the first N inputs are submitted at the SAME moment (tools/batch_run.py with
--concurrency N) and we wait until all N are done. Each level renames its inputs (<name>_L<N>) so the
Idempotency-Key is new and every level does real work. The worker must run with WORKER_CONCURRENCY >= the
highest level (else jobs just queue).

Per level: wall time (first submit -> last job done), each job's turnaround, narration minutes finished per
wall-clock minute (throughput), and - if --memory-csv points to the CSV of tools/sample_memory.ps1 running
at the same time - the highest API / worker / ffmpeg / total memory seen during that level.

    .venv\\Scripts\\python tools\\load_test.py testpack\\perf\\inputs --levels 1,2,3 --out testpack\\outputs\\concurrency ^
        --memory-csv testpack\\logs\\memory_concurrency.csv
"""
import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.batch_run import collect_inputs, run_batch  # noqa: E402

MEMORY_COLUMNS = ("api_mb", "worker_mb", "ffmpeg_mb", "total_mb")


def memory_peaks(csv_path: Path | None, start: datetime, end: datetime) -> dict | None:
    """Highest value of each memory column between start and end (UTC), or None without samples."""
    if csv_path is None or not csv_path.is_file():
        return None
    peaks, samples = {c: 0.0 for c in MEMORY_COLUMNS}, 0
    for _ in range(50):  # the sampler may hold the file for a moment while it writes a line (Windows lock)
        try:
            text = csv_path.read_text(encoding="utf-8-sig")
            break
        except PermissionError:
            time.sleep(0.1)
    else:
        return None
    for row in csv.DictReader(text.splitlines()):
        try:
            at = datetime.fromisoformat(row["time"])  # e.g. 2026-10-17T11:08:27.8894857Z (UTC)
        except (KeyError, ValueError):
            continue
        if start <= at <= end:
            samples += 1
            for c in MEMORY_COLUMNS:
                peaks[c] = max(peaks[c], float(row.get(c) or 0))
    return {**peaks, "samples": samples} if samples else None


def run_level(level: int, inputs: list, out: Path, client: httpx.Client, ratios: list[str] | None,
              memory_csv: Path | None, timeout: float, tag: str = "") -> dict:
    items = []
    for item in inputs[:level]:
        body = dict(item.body)
        items.append(type(item)(name=f"{item.name}_L{level}{tag}", source=item.source, body=body))
    start = datetime.now(timezone.utc)
    t0 = time.perf_counter()
    results = run_batch(items, out / f"level_{level}", client, ratios, concurrency=level, timeout=timeout,
                        poll=1.0, max_poll=3.0)
    wall = round(time.perf_counter() - t0, 2)
    end = datetime.now(timezone.utc)
    done = [r for r in results if r.status == "succeeded"]
    narration = sum(r.narration_seconds or 0 for r in done)
    return {
        "level": level, "started_utc": start.isoformat(), "finished_utc": end.isoformat(), "wall_seconds": wall,
        "jobs": [{"input": r.input, "job_id": r.job_id, "status": r.status, "error_code": r.error_code,
                  "narration_seconds": r.narration_seconds, "turnaround_seconds": r.turnaround_seconds}
                 for r in results],
        "succeeded": len(done),
        "narration_minutes_finished": round(narration / 60, 3),
        "throughput_narration_min_per_wall_min": round((narration / 60) / (wall / 60), 3) if wall else None,
        "jobs_per_hour": round(len(done) * 3600 / wall, 2) if wall else None,
        "memory_peak_mb": memory_peaks(memory_csv, start, end),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Throughput + memory at 1, 2, 3 ... concurrent jobs.")
    parser.add_argument("inputs", type=Path, help="folder of .json/.txt inputs (at least as many as the top level)")
    parser.add_argument("--levels", default="1,2,3")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--token", default="dev-token")
    parser.add_argument("--ratios", default="16:9,9:16")
    parser.add_argument("--memory-csv", type=Path, default=None)
    parser.add_argument("--timeout", type=float, default=3600)
    parser.add_argument("--tag", default="", help="added to every input name (new Idempotency-Keys for a re-run)")
    args = parser.parse_args(argv)

    levels = [int(x) for x in args.levels.split(",") if x.strip()]
    inputs = [it for it in collect_inputs(args.inputs) if it.body is not None]
    if len(inputs) < max(levels):
        print(f"need {max(levels)} text/json inputs, found {len(inputs)}", file=sys.stderr)
        return 2
    ratios = [r.strip() for r in args.ratios.split(",") if r.strip()] or None
    args.out.mkdir(parents=True, exist_ok=True)
    report = {"api": args.api, "ratios": ratios, "inputs": [it.name for it in inputs[:max(levels)]], "levels": []}
    with httpx.Client(base_url=args.api, headers={"Authorization": f"Bearer {args.token}"}, timeout=120) as client:
        for level in levels:
            print(f"level {level}: {level} job(s) at once ...", flush=True)
            result = run_level(level, inputs, args.out, client, ratios, args.memory_csv, args.timeout, args.tag)
            report["levels"].append(result)
            print(f"  wall {result['wall_seconds']} s, {result['succeeded']}/{level} succeeded, throughput "
                  f"{result['throughput_narration_min_per_wall_min']} narration min / wall min, memory "
                  f"{result['memory_peak_mb']}", flush=True)
            (args.out / "results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
