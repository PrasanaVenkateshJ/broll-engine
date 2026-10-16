"""Clip swap (POST /api/v1/broll/jobs/{job_id}/scenes/{scene_index}/swap) through the real API + worker,
with mock adapters only. One full job is made per test, then one scene's clip is swapped."""
import json

import pytest

from app.pipeline import media
from app.pipeline.providers.mock import MockProvider
from tests.conftest import AUTH, OTHER_AUTH
from tests.fake_api import FakeAPI
from tests.helpers import wait_for_job
from tests.integration.test_job_flow import path_of

URL = "/api/v1/broll/jobs"
SCRIPT = ("Saving money is not about earning more. It is about spending wisely. In 2025, household savings grew by "
          "40 percent. Cloud computing now lets small businesses work from anywhere. Trust is earned slowly, one "
          "honest decision at a time.")
RATIOS = ["16:9", "1:1"]


def swap_url(job_id, scene):
    return f"{URL}/{job_id}/scenes/{scene}/swap"


def finished_job(client, script=SCRIPT, ratios=RATIOS):
    job_id = client.post(URL, json={"script": script, "aspect_ratios": ratios}, headers=AUTH).json()["job_id"]
    final, _ = wait_for_job(client, job_id, timeout=180)
    assert final["status"] == "succeeded", final["error"]
    return final


def swap(client, job_id, scene, body, key):
    return client.post(swap_url(job_id, scene), json=body, headers={**AUTH, "Idempotency-Key": key})


def run_swap(client, job_id, scene, body, key):
    resp = swap(client, job_id, scene, body, key)
    assert resp.status_code == 202, resp.json()
    final, _ = wait_for_job(client, resp.json()["job_id"], timeout=180)
    return final


def get_json(client, final, name):
    return json.loads(client.get(path_of(final["result"]["files"][name])).content)


def uids(timeline):
    return [sc["provider"] + ":" + sc["clip_id"] for sc in timeline["scenes"]]


def test_next_best_swap_rerenders_only_one_scene_and_records_history(client, worker, tmp_path):
    original = finished_job(client)
    before = get_json(client, original, "timeline.json")
    target = next(sc["index"] for sc in before["scenes"] if sc["provider"] == "mock")

    final = run_swap(client, original["job_id"], target, {"mode": "next_best"}, "swap-1")
    assert final["status"] == "succeeded", final["error"]
    assert final["input_type"] == "swap" and final["parent_job_id"] == original["job_id"]
    after = get_json(client, final, "timeline.json")
    thumbs = {f"thumb_{i:03d}.jpg" for i in range(len(after["scenes"]))}
    assert set(final["result"]["files"]) == {"video_16x9.mp4", "video_1x1.mp4", "captions.srt", "timeline.json",
                                             "manifest.json"} | thumbs
    # Scene poster frames: the swapped scene gets a new one, the others are copied from version 1.
    for sc in after["scenes"]:
        old_bytes = client.get(path_of(original["result"]["files"][sc["thumbnail"]])).content
        new_bytes = client.get(path_of(final["result"]["files"][sc["thumbnail"]])).content
        assert (old_bytes == new_bytes) == (sc["index"] != target)
    assert after["version"] == 2 and after["root_job_id"] == original["job_id"]
    changed = [i for i, (a, b) in enumerate(zip(uids(before), uids(after))) if a != b]
    assert changed == [target]                          # ONLY that scene got another clip
    assert len(set(uids(after))) == len(uids(after))    # still no clip twice
    for old_sc, new_sc in zip(before["scenes"], after["scenes"]):
        assert (old_sc["start"], old_sc["end"], old_sc["text"]) == (new_sc["start"], new_sc["end"], new_sc["text"])
        assert new_sc["understanding"] == old_sc["understanding"]  # saved understanding reused, not asked again
        if new_sc["index"] != target:  # cached segments reused as they were
            assert {r: f["file"] for r, f in new_sc["segments"].items()} == \
                   {r: f["file"] for r, f in old_sc["segments"].items()}
    new_scene = after["scenes"][target]
    assert new_scene["reason"].startswith("swap next_best") and new_scene["logo_check"]["result"] == "ok"

    manifest = get_json(client, final, "manifest.json")
    (record,) = manifest["swaps"]
    assert record["scene_index"] == target and record["choice"] == "next_best"
    assert record["from"]["uid"] == uids(before)[target] and record["to"]["uid"] == uids(after)[target]
    assert record["segments_rerendered"] == 2 and record["segments_reused"] == 2 * (len(after["scenes"]) - 1)
    assert 0 < record["swap_seconds"] < record["full_render_seconds"]  # faster than the full render
    assert manifest["version"] == 2 and all(q["problems"] == [] for q in manifest["qa"].values())
    for name in ("video_16x9.mp4", "video_1x1.mp4"):
        path = tmp_path / name
        path.write_bytes(client.get(path_of(final["result"]["files"][name])).content)
        assert abs(media.duration_of(path) - after["duration"]) < 0.1

    # The original job's files are untouched; a second swap (given the swap job's id) builds on version 2.
    assert uids(get_json(client, original, "timeline.json")) == uids(before)
    other = next(sc["index"] for sc in after["scenes"] if sc["provider"] == "mock" and sc["index"] != target)
    second = run_swap(client, final["job_id"], other, {}, "swap-2")  # empty body = next_best
    assert second["status"] == "succeeded", second["error"]
    v3 = get_json(client, second, "timeline.json")
    assert v3["version"] == 3 and [s["scene_index"] for s in v3["swaps"]] == [target, other]
    assert uids(v3)[target] == uids(after)[target] and uids(v3)[other] != uids(after)[other]


def test_swap_idempotency_and_required_key(client, worker):
    original = finished_job(client, "Small shops open early. Cloud computing helps them grow.", ["1:1"])
    job_id = original["job_id"]
    first = swap(client, job_id, 0, {"mode": "next_best"}, "same-key")
    again = swap(client, job_id, 0, {"mode": "next_best"}, "same-key")
    assert first.status_code == again.status_code == 202
    assert first.json()["job_id"] == again.json()["job_id"] and again.headers["Idempotent-Replayed"] == "true"
    wait_for_job(client, first.json()["job_id"])

    reused = swap(client, job_id, 1, {"mode": "next_best"}, "same-key")  # same key, different scene
    assert reused.status_code == 409 and reused.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"

    missing = client.post(swap_url(job_id, 0), json={"mode": "next_best"}, headers=AUTH)
    assert missing.status_code == 422 and missing.json()["error"]["details"] == {"header": "Idempotency-Key"}


def test_swap_errors(client, worker, monkeypatch):
    original = finished_job(client, "Small shops open early. Cloud computing helps them grow.", ["1:1"])
    job_id = original["job_id"]

    def code(resp):
        return resp.status_code, resp.json()["error"]["code"]

    assert code(swap(client, "nope", 0, {}, "k1")) == (404, "JOB_NOT_FOUND")
    other_client = client.post(swap_url(job_id, 0), json={}, headers={**OTHER_AUTH, "Idempotency-Key": "k2"})
    assert code(other_client) == (404, "JOB_NOT_FOUND")  # another client's job looks "not found"
    assert code(swap(client, job_id, 99, {}, "k3")) == (404, "SCENE_NOT_FOUND")
    assert code(swap(client, job_id, -1, {}, "k4")) == (404, "SCENE_NOT_FOUND")
    assert code(swap(client, job_id, 0, {"mode": "next_best", "clip_url": "https://cdn.pixabay.com/a.mp4"}, "k5")) \
        == (422, "INVALID_INPUT")
    assert code(swap(client, job_id, 0, {"clip_url": "https://example.com/a.mp4"}, "k6")) == (422, "INVALID_INPUT")
    assert code(swap(client, job_id, 0, {"clip_url": "https://example.com/a.mp4", "user_supplied": True}, "k7")) \
        == (422, "INVALID_INPUT")  # licence fields missing
    assert code(swap(client, job_id, 0, {"provider": "generated", "clip_id": "x"}, "k8")) == (422, "INVALID_INPUT")
    timeline = get_json(client, original, "timeline.json")
    used = next(sc for sc in timeline["scenes"] if sc["provider"] == "mock")
    resp = swap(client, job_id, 0, {"provider": "mock", "clip_id": used["clip_id"]}, "k9")
    assert code(resp) == (422, "INVALID_INPUT") and resp.json()["error"]["details"]["reason"] == "clip_already_used"

    # Footage source down: next_best falls back to a generated card; the next swap has nothing left.
    monkeypatch.setattr(MockProvider, "fail", True)
    scene = used["index"]
    first = run_swap(client, job_id, scene, {}, "k11")
    assert first["status"] == "succeeded", first["error"]
    assert get_json(client, first, "timeline.json")["scenes"][scene]["provider"] == "generated"
    second = run_swap(client, job_id, scene, {}, "k12")
    assert second["status"] == "failed" and second["error"]["code"] == "NO_ALTERNATIVE_CLIP"


def test_swap_with_provider_and_clip_id_and_frame_logo_check(client, worker, monkeypatch):
    original = finished_job(client, "Small shops open early. Cloud computing helps them grow.", ["1:1"])
    job_id = original["job_id"]
    final = run_swap(client, job_id, 0, {"provider": "mock", "clip_id": "abcdef0123"}, "pc-1")
    assert final["status"] == "succeeded", final["error"]
    scene = get_json(client, final, "timeline.json")["scenes"][0]
    assert (scene["provider"], scene["clip_id"]) == ("mock", "abcdef0123")
    assert "chosen by the user" in scene["reason"] and len(scene["logo_check"]["frames"]) == 3

    # A clean id the user picks, but a frame of the downloaded clip shows a logo -> the swap is refused.
    monkeypatch.setattr(MockProvider, "frame_logo_probs", {"abcdef0124": [0.05, 0.9, 0.05]})
    bad = run_swap(client, job_id, 0, {"provider": "mock", "clip_id": "abcdef0124"}, "pc-2")
    assert bad["status"] == "failed" and bad["error"]["code"] == "INVALID_INPUT"
    assert bad["error"]["details"]["reason"] == "logo_check_failed"

    missing = run_swap(client, job_id, 0, {"provider": "mock", "clip_id": "zzz"}, "pc-3")
    assert missing["status"] == "failed" and missing["error"]["code"] == "INVALID_INPUT"


@pytest.fixture
def clip_server(tmp_path):
    clip = tmp_path / "user_clip.mp4"
    media.ffmpeg("-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=4", "-pix_fmt", "yuv420p", "-c:v", "libx264",
                 "-preset", "ultrafast", str(clip))
    server = FakeAPI(lambda method, path, query, body: (200, clip.read_bytes()))
    yield server
    server.close()


def test_swap_with_a_user_supplied_clip_url(client, worker, clip_server):
    original = finished_job(client, "Small shops open early. Cloud computing helps them grow.", ["1:1"])
    body = {"clip_url": f"{clip_server.url}/my-shop.mp4", "user_supplied": True, "licence": "CC0",
            "licence_url": "https://creativecommons.org/publicdomain/zero/1.0/", "attribution": "Me",
            "commercial_use": True}
    final = run_swap(client, original["job_id"], 0, body, "url-1")
    assert final["status"] == "succeeded", final["error"]
    scene = get_json(client, final, "timeline.json")["scenes"][0]
    assert scene["provider"] == "user_supplied" and scene["licence"] == "CC0" and scene["attribution"] == "Me"
    assert scene["commercial_use"] is True
    # The same file again (now in the video) is refused before any work starts.
    again = swap(client, final["job_id"], 0, body, "url-2")
    assert again.status_code == 422 and again.json()["error"]["details"]["reason"] == "clip_already_used"


def test_two_swaps_at_the_same_moment_only_one_starts(client, monkeypatch):
    """The race: two swap requests both pass every check, then both try to create their job.
    A barrier holds both requests at the last check (swap_precheck) until BOTH have arrived, so they
    hit the database at the same moment every run. Only one may get the video's swap lock."""
    import threading

    import app.main
    from app.errors import AppError, ErrorCode
    from app.jobs import finish_job
    from app.models import Job
    from app.db import new_session
    from app.worker import start_workers

    stop = threading.Event()  # own worker: stopped after the full job, so the swap jobs stay queued
    threads = start_workers(stop)
    try:
        original = finished_job(client, "Small shops open early. Cloud computing helps them grow.", ["1:1"])
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=30)
    job_id = original["job_id"]

    barrier = threading.Barrier(2, timeout=20)
    real_precheck = app.main.swap_precheck

    def precheck_together(*args):
        real_precheck(*args)
        barrier.wait()  # both requests are now past every check (the old code's "busy" query included)

    monkeypatch.setattr(app.main, "swap_precheck", precheck_together)
    responses = [None, None]

    def fire(i):
        responses[i] = swap(client, job_id, 0, {"mode": "next_best"}, f"race-{i}")

    shots = [threading.Thread(target=fire, args=(i,)) for i in range(2)]
    for t in shots:
        t.start()
    for t in shots:
        t.join(timeout=60)

    codes = sorted(r.status_code for r in responses)
    assert codes == [202, 409], [r.json() for r in responses]
    winner = next(r for r in responses if r.status_code == 202).json()["job_id"]
    loser = next(r for r in responses if r.status_code == 409).json()["error"]
    assert loser["code"] == "SWAP_IN_PROGRESS" and loser["retryable"] is True
    assert loser["details"] == {"swap_job_id": winner, "root_job_id": job_id}
    with new_session() as db:  # exactly ONE swap job exists, and it holds the lock
        swaps = db.query(Job).filter(Job.parent_job_id == job_id).all()
        assert [j.id for j in swaps] == [winner]
        assert db.get(Job, job_id).active_swap_id == winner

    monkeypatch.setattr(app.main, "swap_precheck", real_precheck)
    busy = swap(client, job_id, 0, {"mode": "next_best"}, "race-later")
    assert busy.status_code == 409 and busy.json()["error"]["code"] == "SWAP_IN_PROGRESS"

    # When the swap ends (here: it fails), the lock is released and the next swap may start.
    finish_job(winner, error=AppError(ErrorCode.RENDER_FAILED, "boom", 500))
    with new_session() as db:
        assert db.get(Job, job_id).active_swap_id is None
    assert swap(client, job_id, 0, {"mode": "next_best"}, "race-after").status_code == 202


def test_a_lock_left_by_a_finished_swap_heals_itself(client):
    """If a swap finished without releasing the lock (e.g. a crash between the two steps), the
    finished holder does not block the video forever."""
    from app.db import new_session
    from app.jobs import create_job, take_swap_lock
    from app.models import Job, JobStatus

    with new_session() as db:
        for jid, status in (("root1", JobStatus.SUCCEEDED), ("oldswap", JobStatus.SUCCEEDED),
                            ("runswap", JobStatus.RUNNING)):
            create_job(db, job_id=jid, client_id="c", idem_key=None, req_hash="h", input_type="script",
                       request_data={})
            db.get(Job, jid).status = status
        db.get(Job, "root1").active_swap_id = "runswap"
        db.commit()
        assert take_swap_lock(db, "root1", "new1") is False  # holder still running -> taken
        db.rollback()
        db.get(Job, "root1").active_swap_id = "oldswap"
        db.commit()
        assert take_swap_lock(db, "root1", "new1") is True   # holder already finished -> free
        db.commit()
        assert db.get(Job, "root1").active_swap_id == "new1"


def test_swap_of_a_queued_or_failed_job_is_job_not_ready(client):
    """No worker fixture here, so the job stays queued."""
    from app.jobs import finish_job
    from app.errors import AppError, ErrorCode

    job_id = client.post(URL, json={"script": "Busy job.", "aspect_ratios": ["1:1"]}, headers=AUTH).json()["job_id"]
    resp = swap(client, job_id, 0, {}, "nr-1")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "JOB_NOT_READY"
    assert resp.json()["error"]["retryable"] is True

    finish_job(job_id, error=AppError(ErrorCode.RENDER_FAILED, "boom", 500))
    resp = swap(client, job_id, 0, {}, "nr-2")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "JOB_NOT_READY"
    assert resp.json()["error"]["retryable"] is False
