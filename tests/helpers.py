"""Small helpers shared by the integration / e2e tests."""
import time

from tests.conftest import AUTH

FIRST_PAUSE = 0.05   # seconds before the 2nd status check
MAX_PAUSE = 0.5      # the pause grows by x1.5 per check up to this
TERMINAL = ("succeeded", "failed")


def wait_for_job(client, job_id, timeout=90, headers=AUTH):
    """Poll GET /api/v1/jobs/{job_id} until the job ends. Returns (final body, list of seen (status, progress)).

    The pause between checks grows (0.05 s, 0.075 s, ... up to 0.5 s), so a slow CI machine does
    not fire thousands of requests. Any answer other than 200 fails at once with its status and
    body (instead of a confusing KeyError on a 429 or 404 error body).
    """
    seen = []
    pause = FIRST_PAUSE
    deadline = time.monotonic() + timeout
    while True:
        resp = client.get(f"/api/v1/jobs/{job_id}", headers=headers)
        if resp.status_code != 200:
            raise AssertionError(f"GET /api/v1/jobs/{job_id} answered HTTP {resp.status_code}: {resp.text}")
        body = resp.json()
        seen.append((body["status"], body["progress"]))
        if body["status"] in TERMINAL:
            return body, seen
        if time.monotonic() >= deadline:
            raise AssertionError(f"job {job_id} did not finish in {timeout} s; last (status, progress) = {seen[-1]}")
        time.sleep(pause)
        pause = min(pause * 1.5, MAX_PAUSE)
