"""Check a running B-roll API (e.g. the Hugging Face Space) from the outside, like a reviewer would.

1. (--cold) how long until GET /ready answers 200 = cold start, measured from when this tool starts.
   For a real cold-start number, start it right after a restart / while the Space is asleep.
2. GET /health and GET /ready (status + answer time).
3. POST one short script job (fresh Idempotency-Key per run), poll it politely (growing pause,
   honours Retry-After), download every output file (same code as tools/batch_run.py).
4. One Range request on the first video: must be 206 Partial Content (seeking in a browser).
5. Prints the turnaround (POST -> succeeded) and the stage timings from manifest.json.

The token comes from --token, $BROLL_API_TOKEN, or a hidden prompt. It is never printed or saved.

    python tools/live_check.py --url https://<hf-username>-broll-studio.hf.space --cold
    python tools/live_check.py --url http://localhost:8000 --token dev-token --ratios 1:1
Exit code 0 = every check passed.
"""
import argparse
import getpass
import json
import os
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # so "tools.batch_run" imports when run as a script

from tools.batch_run import Item, retry_after_seconds, run_one, send  # noqa: E402

DEFAULT_SCRIPT = ("Saving money is not about earning more. It is about spending wisely. "
                  "Small daily habits can cut your monthly costs by 40 percent.")


def wait_until_ready(client: httpx.Client, timeout: float, interval: float = 5.0) -> float | None:
    """Seconds until /ready answered 200 (None = not within timeout). A sleeping or building Space
    answers with errors or an HTML page first; every such answer just means "not yet"."""
    start = time.perf_counter()
    while time.perf_counter() - start < timeout:
        try:
            resp = client.get("/ready", timeout=30)
            if resp.status_code == 200:
                return round(time.perf_counter() - start, 1)
            wait = retry_after_seconds(resp, interval) if resp.status_code == 429 else interval
        except httpx.HTTPError:
            wait = interval
        time.sleep(wait)
    return None


def timed_get(client: httpx.Client, path: str) -> tuple[int, float, str]:
    start = time.perf_counter()
    resp = client.get(path)
    return resp.status_code, round((time.perf_counter() - start) * 1000), resp.text[:200]


def check_range(client: httpx.Client, url: str) -> bool:
    """The first 1 KB of a video: 206 + Content-Range + exactly 1024 bytes = seeking works."""
    resp = send(lambda: client.get(url, headers={"Range": "bytes=0-1023"}))
    ok = resp.status_code == 206 and len(resp.content) == 1024 and resp.headers.get("content-range", "").startswith(
        "bytes 0-1023/")
    print(f"range request: HTTP {resp.status_code}, {len(resp.content)} bytes, "
          f"Content-Range {resp.headers.get('content-range', '-')} -> {'ok' if ok else 'FAILED'}")
    return ok


def run_check(client: httpx.Client, out: Path, script: str, ratios: list[str], timeout: float,
              poll: float = 3.0, max_poll: float = 15.0) -> bool:
    ok = True
    for path in ("/health", "/ready"):
        status, ms, text = timed_get(client, path)
        print(f"GET {path}: HTTP {status} in {ms} ms  {text if status != 200 else ''}".rstrip())
        ok &= status == 200

    item = Item(name=f"live-{time.strftime('%Y%m%d-%H%M%S')}", source="live_check", body={"script": script})
    print(f"job: {len(script.split())} words, ratios {','.join(ratios)} ... (polling politely)")
    result = run_one(client, item, out, ratios, timeout, poll, max_poll)
    print(f"job {result.job_id or '-'}: {result.status} {result.error_code}".rstrip())
    if result.status != "succeeded":
        print(f"details: {out / item.name / 'job.json'}")
        return False
    print(f"turnaround (POST -> succeeded): {result.turnaround_seconds} s for {result.narration_seconds} s "
          f"of narration, {result.scenes} scenes, {result.warnings_count} warnings")
    print(f"downloaded {len(result.files)} files to {out / item.name}")

    job = json.loads((out / item.name / "job.json").read_text(encoding="utf-8"))
    files = job["result"]["files"]
    expected = {f"video_{r.replace(':', 'x')}.mp4" for r in ratios} | {"captions.srt", "timeline.json",
                                                                         "manifest.json"}
    missing = expected - set(result.files)
    if missing:
        print(f"MISSING outputs: {sorted(missing)}")
        ok = False
    videos = sorted(n for n in files if n.endswith(".mp4"))
    if videos:
        ok &= check_range(client, files[videos[0]])
    manifest = out / item.name / "manifest.json"
    if manifest.is_file():
        timings = json.loads(manifest.read_text(encoding="utf-8")).get("timings_seconds") or {}
        if timings:
            print("stage timings (manifest.json): " + json.dumps(timings))
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a live B-roll API: health, ready, one job, turnaround.")
    parser.add_argument("--url", default=os.environ.get("BROLL_API_URL", ""),
                        help="base URL, e.g. https://<hf-username>-broll-studio.hf.space (default: $BROLL_API_URL)")
    parser.add_argument("--token", default=os.environ.get("BROLL_API_TOKEN", ""),
                        help="bearer token (default: $BROLL_API_TOKEN, else a hidden prompt)")
    parser.add_argument("--cold", action="store_true", help="first measure the time until /ready answers")
    parser.add_argument("--cold-timeout", type=float, default=1800, help="max seconds to wait for /ready")
    parser.add_argument("--script", default=DEFAULT_SCRIPT, help="text of the test job")
    parser.add_argument("--ratios", default="16:9,9:16", help="aspect ratios of the test job (default 16:9,9:16)")
    parser.add_argument("--timeout", type=float, default=1200, help="max seconds to wait for the job")
    parser.add_argument("--out", type=Path, default=Path("data") / "live_check", help="where outputs are saved")
    args = parser.parse_args(argv)
    if not args.url:
        parser.error("--url is required (or set BROLL_API_URL)")
    token = args.token or getpass.getpass("API token (input hidden): ").strip()
    ratios = [r.strip() for r in args.ratios.split(",") if r.strip()]

    with httpx.Client(base_url=args.url.rstrip("/"), headers={"Authorization": f"Bearer {token}"}, timeout=120,
                      follow_redirects=True) as client:
        if args.cold:
            print(f"waiting for {args.url}/ready (cold start) ...")
            seconds = wait_until_ready(client, args.cold_timeout)
            if seconds is None:
                print(f"cold start: /ready did not answer 200 within {args.cold_timeout:.0f} s")
                return 1
            print(f"cold start: /ready answered after {seconds} s")
        ok = run_check(client, args.out, args.script, ratios, args.timeout)
    print("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
