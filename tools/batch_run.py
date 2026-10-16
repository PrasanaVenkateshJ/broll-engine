"""Batch runner: send many inputs to the B-roll API and collect every result in one folder.

Input (first argument):
- a FOLDER with any mix of
    *.mp3 / *.wav / *.m4a  -> uploaded as the voiceover (multipart),
    *.txt                  -> {"script": <the text>},
    *.json                 -> a full request body (e.g. {"scenes": [...]}),
- or a .jsonl FILE: one request body per line.
A body may hold "name": "my-test" (used as the output folder name; removed before sending).

For each input: POST with an Idempotency-Key (made from the input itself, so running the batch
again never makes duplicate jobs), poll GET /api/v1/jobs/{id} until it ends, then download every
output file into --out/<input-name>/ together with job.json (the final job answer).
At the end: --out/summary.csv and --out/summary.md.

    python tools/batch_run.py testpack/inputs --out testpack/outputs/run1
    python tools/batch_run.py inputs.jsonl --out out --api https://my-space.hf.space --token XYZ --ratios 16:9,9:16
"""
import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import httpx

AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a"}
CSV_FIELDS = ["input", "job_id", "status", "error_code", "narration_seconds", "turnaround_seconds", "scenes",
              "warnings_count"]
TERMINAL = ("succeeded", "failed")


@dataclass
class Item:
    name: str                    # output folder name (unique)
    source: str                  # where it came from, e.g. "inputs/story.txt" or "list.jsonl:3"
    body: dict | None = None     # JSON request body (None for an audio upload)
    audio: Path | None = None    # audio file to upload


@dataclass
class Result:
    input: str
    job_id: str = ""
    status: str = ""
    error_code: str = ""
    narration_seconds: float | None = None
    turnaround_seconds: float | None = None
    scenes: int | None = None
    warnings_count: int = 0
    files: list[str] = field(default_factory=list)

    def row(self) -> dict:
        return {k: ("" if getattr(self, k) is None else getattr(self, k)) for k in CSV_FIELDS}


# ---------- Reading the inputs ----------

def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "input"


def _from_body(body: dict, default_name: str, source: str) -> Item:
    if not isinstance(body, dict):
        raise ValueError(f"{source}: a request body must be a JSON object")
    body = dict(body)
    return Item(name=_safe(str(body.pop("name", "") or default_name)), source=source, body=body)


def _items_of_file(f: Path) -> list[Item] | None:
    """The input(s) in one file, or None if the file type is not an input."""
    ext = f.suffix.lower()
    if ext in AUDIO_EXTENSIONS:
        return [Item(name=_safe(f.stem), source=str(f), audio=f)]
    if ext == ".txt":
        return [Item(name=_safe(f.stem), source=str(f), body={"script": f.read_text(encoding="utf-8")})]
    if ext == ".json":
        return [_from_body(json.loads(f.read_text(encoding="utf-8")), f.stem, str(f))]
    if ext == ".jsonl":
        return [_from_body(json.loads(line), f"{f.stem}-{n:03d}", f"{f}:{n}")
                for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), start=1) if line.strip()]
    return None


def collect_inputs(path: Path) -> list[Item]:
    """Every input in a folder (sorted by file name), or in one file (.jsonl = one input per line)."""
    if not path.exists():
        raise FileNotFoundError(path)
    items: list[Item] = []
    for f in sorted(p for p in path.iterdir() if p.is_file()) if path.is_dir() else [path]:
        found = _items_of_file(f)
        if found is None:
            print(f"skipped {f.name} (not audio, .txt, .json or .jsonl)", file=sys.stderr)
        else:
            items += found
    # Two inputs with the same name ("a.txt" and "a.json") must not share an output folder.
    seen: dict[str, int] = {}
    for item in items:
        if item.name in seen:
            seen[item.name] += 1
            item.name = f"{item.name}-{seen[item.name]}"
        else:
            seen[item.name] = 1
    return items


def idempotency_key(item: Item, ratios: list[str] | None) -> str:
    """Same input (+ same --ratios) -> same key -> the API returns the same job instead of new work."""
    digest = hashlib.sha256()
    digest.update(item.name.encode())
    digest.update(json.dumps(item.body, sort_keys=True).encode() if item.body is not None else b"")
    if item.audio is not None:
        digest.update(item.audio.read_bytes())
    digest.update(json.dumps(ratios).encode())
    return f"batch-{digest.hexdigest()[:40]}"


# ---------- Running one input ----------

def _error_code(resp: httpx.Response) -> str:
    try:
        return resp.json()["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return f"HTTP_{resp.status_code}"


def run_one(client: httpx.Client, item: Item, out: Path, ratios: list[str] | None, timeout: float,
            poll: float) -> Result:
    result = Result(input=item.name)
    folder = out / item.name
    folder.mkdir(parents=True, exist_ok=True)
    headers = {"Idempotency-Key": idempotency_key(item, ratios), "X-Correlation-Id": f"batch-{item.name}"[:128]}
    started = time.perf_counter()
    try:
        if item.audio is not None:
            options = {"aspect_ratios": ratios} if ratios else {}
            with item.audio.open("rb") as f:
                resp = client.post("/api/v1/broll/jobs", headers=headers, data={"options": json.dumps(options)},
                                   files={"audio": (item.audio.name, f, "application/octet-stream")})
        else:
            body = {**item.body, "aspect_ratios": ratios} if ratios else item.body
            resp = client.post("/api/v1/broll/jobs", headers=headers, json=body)
        if resp.status_code != 202:  # refused straight away (e.g. EMPTY_SCRIPT, UNSUPPORTED_FILE_TYPE)
            result.status, result.error_code = "rejected", _error_code(resp)
            (folder / "job.json").write_text(resp.text, encoding="utf-8")
            return result
        result.job_id = resp.json()["job_id"]
        job = {}
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            r = client.get(f"/api/v1/jobs/{result.job_id}")
            if r.status_code == 200:
                job = r.json()
                if job["status"] in TERMINAL:
                    break
            time.sleep(poll)
        result.turnaround_seconds = round(time.perf_counter() - started, 2)
        (folder / "job.json").write_text(json.dumps(job, indent=2), encoding="utf-8")
        result.status = job.get("status") if job.get("status") in TERMINAL else "timeout"
        result.warnings_count = len(job.get("warnings") or [])
        if job.get("error"):
            result.error_code = job["error"].get("code", "")
        for name, url in ((job.get("result") or {}).get("files") or {}).items():
            data = client.get(url)  # signed link: works without the bearer token too
            if data.status_code == 200:
                (folder / name).write_bytes(data.content)
                result.files.append(name)
            else:
                print(f"{item.name}: could not download {name} (HTTP {data.status_code})", file=sys.stderr)
        timeline = folder / "timeline.json"
        if timeline.is_file():
            data = json.loads(timeline.read_text(encoding="utf-8"))
            result.narration_seconds = data.get("duration")
            result.scenes = len(data.get("scenes") or [])
    except (httpx.HTTPError, OSError, ValueError, KeyError) as e:
        result.status = result.status or "error"
        result.error_code = result.error_code or type(e).__name__
        print(f"{item.name}: {type(e).__name__}: {e}", file=sys.stderr)
    return result


# ---------- Summary ----------

def write_summary(results: list[Result], out: Path, api: str) -> None:
    with (out / "summary.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for r in results:
            writer.writerow(r.row())
    done = [r for r in results if r.status == "succeeded"]
    turnarounds = [r.turnaround_seconds for r in done if r.turnaround_seconds is not None]
    lines = [
        "# Batch run summary", "",
        f"- API: {api}",
        f"- Inputs: {len(results)} | succeeded: {len(done)} | failed / rejected / other: {len(results) - len(done)}",
        f"- Narration total (succeeded): {sum(r.narration_seconds or 0 for r in done):.1f} s",
        f"- Turnaround of succeeded jobs: mean {sum(turnarounds) / len(turnarounds):.1f} s, "
        f"max {max(turnarounds):.1f} s" if turnarounds else "- Turnaround: no succeeded jobs",
        "",
        "| " + " | ".join(CSV_FIELDS) + " |",
        "|" + "---|" * len(CSV_FIELDS),
    ]
    lines += ["| " + " | ".join(str(v) for v in r.row().values()) + " |" for r in results]
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_batch(items: list[Item], out: Path, client: httpx.Client, ratios: list[str] | None = None,
              concurrency: int = 2, timeout: float = 1800, poll: float = 2.0, api: str = "") -> list[Result]:
    out.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        results = list(pool.map(lambda it: run_one(client, it, out, ratios, timeout, poll), items))
    write_summary(results, out, api or str(client.base_url))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a folder or .jsonl file of inputs against the B-roll API.")
    parser.add_argument("input", type=Path, help="folder (audio / .txt / .json files) or a .jsonl file")
    parser.add_argument("--out", type=Path, required=True, help="results folder (one sub-folder per input)")
    parser.add_argument("--api", default=os.environ.get("BROLL_API_URL", "http://localhost:8000"),
                        help="API base URL (default: $BROLL_API_URL or http://localhost:8000)")
    parser.add_argument("--token", default=os.environ.get("BROLL_API_TOKEN", "dev-token"),
                        help="bearer token (default: $BROLL_API_TOKEN or dev-token)")
    parser.add_argument("--ratios", default="", help="e.g. 16:9,9:16 (overrides each input's aspect_ratios)")
    parser.add_argument("--concurrency", type=int, default=2, help="inputs running at the same time (default 2)")
    parser.add_argument("--timeout", type=float, default=1800, help="max seconds to wait for one job")
    parser.add_argument("--poll", type=float, default=2.0, help="seconds between status checks")
    args = parser.parse_args(argv)

    items = collect_inputs(args.input)
    if not items:
        print("no inputs found", file=sys.stderr)
        return 2
    ratios = [r.strip() for r in args.ratios.split(",") if r.strip()] or None
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    print(f"{len(items)} input(s) -> {args.api} (concurrency {args.concurrency})")
    with httpx.Client(base_url=args.api, headers=headers, timeout=120, follow_redirects=True) as client:
        results = run_batch(items, args.out, client, ratios, args.concurrency, args.timeout, args.poll, args.api)
    for r in results:
        print(f"{r.input:30} {r.status:10} {r.error_code:24} {r.turnaround_seconds or '':>8} s  job {r.job_id}")
    print(f"summary: {args.out / 'summary.csv'} and {args.out / 'summary.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
