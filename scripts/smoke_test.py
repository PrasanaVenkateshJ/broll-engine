"""Smoke test against a RUNNING stack (local scripts, Docker or deployed).

Submits one job, polls until it ends, downloads the result through the signed link.
Run:  python scripts/smoke_test.py            (uses SMOKE_BASE_URL and the first API_TOKENS entry)
Exit code 0 = everything worked.
"""
import os
import sys
import time
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import get_settings  # noqa: E402


def main() -> int:
    base = os.environ.get("SMOKE_BASE_URL", "http://localhost:8000").rstrip("/")
    tokens = get_settings().api_tokens
    headers = {"Authorization": f"Bearer {tokens[0]}"} if tokens else {}

    with httpx.Client(base_url=base, timeout=30) as http:
        ready = http.get("/ready")
        print("ready:", ready.status_code, ready.json())
        if ready.status_code != 200:
            return 1

        key = f"smoke-{uuid.uuid4().hex}"
        body = {"script": "Cloud computing changed how teams work.", "aspect_ratios": ["16:9", "9:16"]}
        first = http.post("/api/v1/broll/jobs", json=body, headers={**headers, "Idempotency-Key": key})
        print("submit:", first.status_code, first.json())
        if first.status_code != 202:
            return 1
        again = http.post("/api/v1/broll/jobs", json=body, headers={**headers, "Idempotency-Key": key})
        if again.json().get("job_id") != first.json()["job_id"]:
            print("FAIL: idempotent retry created a different job")
            return 1

        job_id = first.json()["job_id"]
        deadline = time.time() + 120
        while time.time() < deadline:
            job = http.get(f"/api/v1/jobs/{job_id}", headers=headers).json()
            print(f"  status={job['status']} progress={job['progress']} stage={job['stage']}")
            if job["status"] in ("succeeded", "failed"):
                break
            time.sleep(1)
        if job["status"] != "succeeded":
            print("FAIL: job did not succeed:", job.get("error"))
            return 1

        for name, link in job["result"]["files"].items():
            resp = httpx.get(link, timeout=30)  # no token: the signed link is enough
            print(f"download {name}: HTTP {resp.status_code}, {len(resp.content)} bytes")
            if resp.status_code != 200:
                return 1
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
