"""tools/live_check.py against the real API + worker (TestClient is an httpx.Client, like a live
server). Mock adapters, no keys. The cold-start wait is tested with a fake API."""
import httpx

import tools.live_check as live
from tests.conftest import AUTH


def test_live_check_passes_against_the_app(client, worker, tmp_path, capsys):
    client.headers.update(AUTH)
    ok = live.run_check(client, tmp_path, "Small shops open early. Sales grew 40 percent.", ["1:1"], timeout=180,
                        poll=0.05)
    out = capsys.readouterr().out
    assert ok, out
    assert "GET /health: HTTP 200" in out and "GET /ready: HTTP 200" in out
    assert "range request: HTTP 206, 1024 bytes" in out  # seeking works
    assert "turnaround (POST -> succeeded):" in out
    (folder,) = tmp_path.glob("live-*")  # one folder per run, named after the time
    assert {"video_1x1.mp4", "captions.srt", "timeline.json", "manifest.json", "job.json"} <= {
        p.name for p in folder.iterdir()}


def test_live_check_fails_when_the_job_fails(client, worker, tmp_path, capsys):
    client.headers.update(AUTH)
    assert not live.run_check(client, tmp_path, "   ", ["1:1"], timeout=60, poll=0.05)  # EMPTY_SCRIPT
    assert "rejected EMPTY_SCRIPT" in capsys.readouterr().out


def test_wait_until_ready_counts_until_the_first_200(monkeypatch):
    """A waking Space: connection error, 503, 429 + Retry-After, then 200."""
    clock = {"t": 0.0}
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        clock["t"] += s

    monkeypatch.setattr(live.time, "sleep", sleep)
    monkeypatch.setattr(live.time, "perf_counter", lambda: clock["t"])
    answers = iter([httpx.ConnectError("asleep"), httpx.Response(503), httpx.Response(429, headers={"Retry-After": "12"}),
                    httpx.Response(200, json={"status": "ready"})])

    def handler(request):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    with httpx.Client(base_url="http://space", transport=httpx.MockTransport(handler)) as c:
        assert live.wait_until_ready(c, timeout=600, interval=5) == 22.0
    assert sleeps == [5, 5, 12.0]


def test_wait_until_ready_gives_up_after_the_timeout(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(live.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    monkeypatch.setattr(live.time, "perf_counter", lambda: clock["t"])
    with httpx.Client(base_url="http://space", transport=httpx.MockTransport(lambda r: httpx.Response(503))) as c:
        assert live.wait_until_ready(c, timeout=20, interval=5) is None
