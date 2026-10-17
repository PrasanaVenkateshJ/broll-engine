"""Resilience tests against the RUNNING local API + worker. Each test writes its own output folder with the
job's files (tools/batch_run.py layout) and events.json: what the test did and when (UTC).

switch-off   Start a job, wait until the footage stage has picked its first scene, then switch a provider off
             with the override file (PROVIDERS_OVERRIDE_FILE, re-read before every search). The job must still
             finish, and the scenes chosen after the switch must not use that provider. The override file
             is removed again at the end (also if the test fails).
two-users    Two jobs posted at the same moment with two DIFFERENT bearer tokens. Both must finish, and each user
             must NOT be able to read the other user's job.

The other two resilience tests need a special worker, not a special client (see testpack/resilience/README.md):
corrupted download -> tools/fault_worker.py; Groq outage -> the worker started with a bad GROQ_API_KEY.

    .venv\\Scripts\\python tools\\resilience.py switch-off testpack\\resilience\\inputs\\res_a_switch_off.json ^
        --out testpack\\outputs\\resilience\\a_switch_off --provider pixabay_video
    .venv\\Scripts\\python tools\\resilience.py two-users testpack\\resilience\\inputs\\res_c_user_a.json ^
        testpack\\resilience\\inputs\\res_c_user_b.json --tokens dev-token,eval-user-b --out testpack\\outputs\\resilience\\c_two_users
"""
import argparse
import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from tools.batch_run import collect_inputs, run_one, write_summary  # noqa: E402


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Events:
    def __init__(self, out: Path):
        self.out, self.items = out, []

    def add(self, what: str, **facts) -> None:
        self.items.append({"time_utc": now(), "event": what, **facts})
        print(f"{self.items[-1]['time_utc']}  {what}  {facts or ''}", flush=True)
        (self.out / "events.json").write_text(json.dumps(self.items, indent=2), encoding="utf-8")


def newest_job_since(client: httpx.Client, since: str) -> dict | None:
    resp = client.get("/api/v1/jobs", params={"limit": 5})
    if resp.status_code != 200:
        return None
    jobs = [j for j in resp.json().get("jobs", []) if j["created_at"] >= since]
    return jobs[0] if jobs else None


def switch_off(args) -> int:
    item = collect_inputs(args.input)[0]
    args.out.mkdir(parents=True, exist_ok=True)
    events = Events(args.out)
    override = Path(get_settings().PROVIDERS_OVERRIDE_FILE)
    if override.exists():
        print(f"{override} already exists - remove it first", file=sys.stderr)
        return 2
    headers = {"Authorization": f"Bearer {args.token}"}
    with httpx.Client(base_url=args.api, headers=headers, timeout=120) as client:
        result = {}
        started = now()
        runner = threading.Thread(target=lambda: result.update(
            r=run_one(client, item, args.out, args.ratios.split(","), timeout=3600, poll=1.0, max_poll=3.0)))
        runner.start()
        events.add("job submitted", input=item.name)
        try:
            job, switched = None, False
            while runner.is_alive():
                job = job or newest_job_since(client, started)
                if job:
                    state = client.get(f"/api/v1/jobs/{job['job_id']}").json()
                    if not switched and state.get("stage") == "footage" and state.get("progress", 0) > 60:
                        override.parent.mkdir(parents=True, exist_ok=True)
                        override.write_text(json.dumps({"disabled": [args.provider]}), encoding="utf-8")
                        switched = True
                        events.add("provider switched OFF via override file", provider=args.provider,
                                   file=str(override), job_id=job["job_id"], job_stage=state.get("stage"),
                                   job_progress=state.get("progress"))
                    if state.get("status") in ("succeeded", "failed"):
                        break
                time.sleep(0.25)
            runner.join()
            if not switched:
                events.add("WARNING: the job left the footage stage before the switch could happen")
        finally:
            if override.exists():
                override.unlink()
                events.add("override file removed (provider back on)", file=str(override))
        r = result["r"]
        write_summary([r], args.out, args.api)
        events.add("job ended", job_id=r.job_id, status=r.status, error_code=r.error_code,
                   turnaround_seconds=r.turnaround_seconds)
        timeline = args.out / item.name / "timeline.json"
        if timeline.is_file():
            scenes = json.loads(timeline.read_text(encoding="utf-8"))["scenes"]
            events.add("scene sources", scenes=[{"index": sc["index"], "provider": sc["provider"],
                                                 "switched_off_note": f"{args.provider} skipped (switched off"
                                                 in (sc.get("reason") or "")}
                                                for sc in scenes])
    return 0 if r.status == "succeeded" else 1


def two_users(args) -> int:
    items = [collect_inputs(p)[0] for p in args.inputs]
    tokens = [t.strip() for t in args.tokens.split(",")]
    if len(items) != 2 or len(tokens) != 2:
        print("two inputs and two tokens are needed", file=sys.stderr)
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    events = Events(args.out)
    clients = [httpx.Client(base_url=args.api, headers={"Authorization": f"Bearer {t}"}, timeout=120) for t in tokens]
    results: list = [None, None]
    barrier = threading.Barrier(2)

    def go(i: int) -> None:
        barrier.wait()  # both POSTs leave at the same moment
        results[i] = run_one(clients[i], items[i], args.out, args.ratios.split(","), timeout=3600, poll=1.0,
                             max_poll=3.0)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(2)]
    events.add("two users submit at the same moment", users=[f"user {i + 1} (token #{i + 1})" for i in range(2)],
               inputs=[it.name for it in items])
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    write_summary(results, args.out, args.api)
    for i, r in enumerate(results):
        events.add(f"user {i + 1} job ended", job_id=r.job_id, status=r.status, turnaround_seconds=r.turnaround_seconds)
    # Isolation: each user asks for the OTHER user's job.
    for i in range(2):
        other = results[1 - i]
        if other.job_id:
            resp = clients[i].get(f"/api/v1/jobs/{other.job_id}")
            events.add(f"user {i + 1} reads user {2 - i}'s job", http_status=resp.status_code,
                       error_code=(resp.json().get("error") or {}).get("code") if resp.headers.get(
                           "content-type", "").startswith("application/json") else None)
    # Overlap: did the two jobs really run at the same time? (from each job's own start/finish times)
    spans = []
    for r in results:
        job = json.loads((args.out / r.input / "job.json").read_text(encoding="utf-8")) if r.job_id else {}
        spans.append((job.get("started_at"), job.get("finished_at")))
    events.add("job run times (started_at, finished_at)", spans=spans)
    for c in clients:
        c.close()
    return 0 if all(r.status == "succeeded" for r in results) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resilience tests against the running API + worker.")
    sub = parser.add_subparsers(dest="test", required=True)
    a = sub.add_parser("switch-off")
    a.add_argument("input", type=Path)
    a.add_argument("--provider", default="pixabay_video")
    a.add_argument("--token", default="dev-token")
    c = sub.add_parser("two-users")
    c.add_argument("inputs", type=Path, nargs=2)
    c.add_argument("--tokens", required=True, help="two bearer tokens, comma-separated")
    for p in (a, c):
        p.add_argument("--out", type=Path, required=True)
        p.add_argument("--api", default="http://localhost:8000")
        p.add_argument("--ratios", default="16:9,9:16")
    args = parser.parse_args(argv)
    return switch_off(args) if args.test == "switch-off" else two_users(args)


if __name__ == "__main__":
    sys.exit(main())
