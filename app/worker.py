"""The worker: a separate process that does the slow work.

Loop: claim a queued job -> run the pipeline (or a clip swap) -> save the result -> send the callback.
WORKER_CONCURRENCY threads run this loop side by side, so N jobs can run at once.

Run it with:  python -m app.worker
"""
import logging
import os
import signal
import socket
import threading
from collections.abc import Callable

from app.config import get_settings
from app.db import init_db, new_session
from app.errors import AppError, ErrorCode
from app.jobs import claim_next_job, finish_job, send_callback
from app.logging_setup import correlation_id_var, job_id_var, setup_logging
from app.models import Job
from app.pipeline.rank import ClipScorer
from app.pipeline.runner import run_pipeline
from app.pipeline.swap import run_swap

log = logging.getLogger("app.worker")


def process_job(job_id: str) -> None:
    """Run one claimed job from start to end. Never raises: every failure is saved on the job."""
    token = job_id_var.set(job_id)  # every log line from here on carries this job_id
    cid_token = correlation_id_var.set(None)
    try:
        with new_session() as db:
            job = db.get(Job, job_id)
            request, input_type, stored_input = job.request, job.input_type, job.input_key
            correlation_id_var.set(job.correlation_id)  # ...and the id of the request that created it
        log.info("job started")
        try:
            if input_type == "swap":
                files = run_swap(job_id, request)
            else:
                files = run_pipeline(job_id, request, input_type, stored_input)
            finish_job(job_id, files=files)
        except AppError as e:
            finish_job(job_id, error=e)
        except Exception:
            log.exception("job crashed")
            finish_job(job_id, error=AppError(ErrorCode.INTERNAL_ERROR, "Unexpected error while processing the job",
                                              500, retryable=True))
        try:
            send_callback(job_id)
        except Exception:
            log.exception("callback crashed")
    finally:
        job_id_var.reset(token)
        correlation_id_var.reset(cid_token)


def worker_loop(worker_id: str, stop: threading.Event, after_job: Callable[[str], None] | None = None) -> None:
    """after_job(job_id) runs after every finished job (the Modal demo saves its Volume there)."""
    poll = get_settings().WORKER_POLL_SECONDS
    while not stop.is_set():
        try:
            job_id = claim_next_job(worker_id)
        except Exception:
            log.exception("claim failed; retrying")
            job_id = None
        if job_id:
            process_job(job_id)
            if after_job is not None:
                try:
                    after_job(job_id)
                except Exception:
                    log.exception("after_job hook failed")  # never stops the worker
        else:
            stop.wait(poll)  # nothing to do: wait a moment (wakes early on shutdown)


def start_workers(stop: threading.Event, after_job: Callable[[str], None] | None = None) -> list[threading.Thread]:
    """Start WORKER_CONCURRENCY threads. Used by main(), the Modal demo and the integration tests."""
    n = max(1, get_settings().WORKER_CONCURRENCY)
    base = f"{socket.gethostname()}-{os.getpid()}"
    threads = []
    for i in range(n):
        t = threading.Thread(target=worker_loop, args=(f"{base}-{i}", stop, after_job), name=f"worker-{i}", daemon=True)
        t.start()
        threads.append(t)
    return threads


def main() -> None:
    s = get_settings()
    setup_logging(s.LOG_LEVEL)
    init_db()
    stop = threading.Event()
    # Ctrl+C or "docker stop" -> finish the current job, then exit.
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    if s.RANK_SCORER == "clip" and s.CLIP_PRELOAD:
        # Load CLIP once for this process, in the background; a job that needs it first just waits for it.
        threading.Thread(target=ClipScorer.preload, name="clip-preload", daemon=True).start()
    threads = start_workers(stop)
    log.info("worker started", extra={"fields": {"concurrency": len(threads), "storage": s.STORAGE_BACKEND}})
    while not stop.is_set():
        stop.wait(1)
    for t in threads:
        t.join()
    log.info("worker stopped")


if __name__ == "__main__":
    main()
