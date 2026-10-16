"""The job system: create jobs (with idempotency), claim them, update progress, finish them,
and tell the caller with a signed callback.
"""
import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from app.config import get_settings
from app.db import new_session
from app.errors import AppError, ErrorCode
from app.logging_setup import correlation_id_var
from app.models import IdempotencyKey, Job, JobStatus, utcnow
from app.security import hmac_sha256_hex, sign_file_link

log = logging.getLogger("app.jobs")

SIGNATURE_HEADER = "X-IncuBrix-Signature"


def new_job_id() -> str:
    return uuid.uuid4().hex


def iso(dt: datetime | None) -> str | None:
    """Datetime -> ISO text in UTC (SQLite returns datetimes without a timezone)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


# ---------- Idempotency ----------

def body_hash(payload: dict[str, Any], file_sha256: str | None = None) -> str:
    """Fingerprint of a request. Keys are sorted so {"a":1,"b":2} == {"b":2,"a":1}."""
    canonical = json.dumps({"body": payload, "file_sha256": file_sha256}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def find_existing_job(db: Session, client_id: str, idem_key: str | None, req_hash: str) -> Job | None:
    """If this client already used this Idempotency-Key, return that job.

    Same key + same body      -> the old job (no new work).
    Same key + different body -> 409 IDEMPOTENCY_KEY_REUSED.
    """
    if not idem_key:
        return None
    row = db.get(IdempotencyKey, (client_id, idem_key))
    if row is None:
        return None
    if row.body_hash != req_hash:
        raise AppError(ErrorCode.IDEMPOTENCY_KEY_REUSED,
                       "This Idempotency-Key was already used with a different request body", 409,
                       details={"idempotency_key": idem_key, "job_id": row.job_id})
    return db.get(Job, row.job_id)


def create_job(db: Session, *, job_id: str, client_id: str, idem_key: str | None, req_hash: str,
               input_type: str, request_data: dict[str, Any], input_key: str | None = None,
               parent_job_id: str | None = None) -> tuple[Job, bool]:
    """Insert a new queued job (and its idempotency key) in ONE transaction.

    Returns (job, created). created=False means another request with the same key won the
    race a moment earlier; we then return that job instead.
    """
    existing = find_existing_job(db, client_id, idem_key, req_hash)
    if existing is not None:
        return existing, False

    if parent_job_id and not take_swap_lock(db, parent_job_id, job_id):
        db.rollback()
        # Same key + same body sent twice at once: the other request won the lock AND saved the
        # key (it committed before our UPDATE could run), so this is its job, not a second swap.
        existing = find_existing_job(db, client_id, idem_key, req_hash)
        if existing is not None:
            return existing, False
        holder = db.scalar(select(Job.active_swap_id).where(Job.id == parent_job_id))
        raise AppError(ErrorCode.SWAP_IN_PROGRESS, f"Another clip swap ({holder}) is still queued or running for "
                       "this video; one swap at a time, try again when it has finished", 409, retryable=True,
                       details={"swap_job_id": holder, "root_job_id": parent_job_id})

    job = Job(
        id=job_id,
        client_id=client_id,
        status=JobStatus.QUEUED,
        progress=0,
        stage="queued",
        input_type=input_type,
        request_json=json.dumps(request_data),
        input_key=input_key,
        parent_job_id=parent_job_id,
        callback_url=request_data.get("callback_url"),
        correlation_id=correlation_id_var.get(),
    )
    db.add(job)
    if idem_key:
        db.add(IdempotencyKey(client_id=client_id, key=idem_key, body_hash=req_hash, job_id=job_id))
    try:
        db.commit()
    except IntegrityError:
        # Two identical requests at the same moment: the primary key on
        # (client_id, key) lets only one insert win. The loser returns the winner's job.
        db.rollback()
        existing = find_existing_job(db, client_id, idem_key, req_hash)
        if existing is None:
            raise
        return existing, False
    log.info("job created", extra={"job_id": job_id, "fields": {"input_type": input_type}})
    return job, True


# ---------- One clip swap at a time per video ----------

def take_swap_lock(db: Session, root_job_id: str, swap_job_id: str) -> bool:
    """Try to make swap_job_id the ONLY active swap of this video. True = we got the lock.

    One conditional UPDATE: "set active_swap_id = me WHERE it is free". The database runs one
    write at a time, so when two swaps arrive at the same moment only ONE update finds the lock
    free (rowcount 1); the other finds it taken (rowcount 0). A check-then-insert in Python could
    let both through. "Free" also covers a holder that has already finished (a lock left behind
    by a crash), so a stuck lock heals itself. Not committed here: create_job() commits the lock
    together with the new swap job, so either both are saved or neither.
    """
    holder = aliased(Job)
    finished = select(holder.id).where(holder.status.in_([JobStatus.SUCCEEDED, JobStatus.FAILED]))
    result = db.execute(
        update(Job)
        .where(Job.id == root_job_id, or_(Job.active_swap_id.is_(None), Job.active_swap_id.in_(finished)))
        .values(active_swap_id=swap_job_id)
    )
    return result.rowcount == 1


def release_swap_lock(db: Session, root_job_id: str, swap_job_id: str) -> None:
    """Free the lock - only if this swap still holds it (never frees someone else's lock)."""
    db.execute(update(Job).where(Job.id == root_job_id, Job.active_swap_id == swap_job_id)
               .values(active_swap_id=None))


# ---------- Worker side ----------

def claim_next_job(worker_id: str) -> str | None:
    """Atomically take the oldest queued job. Returns its id, or None if there is none.

    The trick: UPDATE ... WHERE id = X AND status = 'queued'. The database runs one
    UPDATE at a time, so if two workers try the same job only ONE sees rowcount == 1;
    the other sees 0 and simply tries the next job.
    """
    with new_session() as db:
        candidates = db.scalars(
            select(Job.id).where(Job.status == JobStatus.QUEUED).order_by(Job.created_at).limit(5)
        ).all()
        for job_id in candidates:
            now = utcnow()
            result = db.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == JobStatus.QUEUED)
                .values(status=JobStatus.RUNNING, stage="starting", worker_id=worker_id,
                        started_at=now, updated_at=now)
            )
            db.commit()
            if result.rowcount == 1:
                return job_id
    return None


def set_progress(job_id: str, progress: int, stage: str) -> None:
    with new_session() as db:
        db.execute(update(Job).where(Job.id == job_id)
                   .values(progress=max(0, min(100, progress)), stage=stage, updated_at=utcnow()))
        db.commit()


def add_warning(job_id: str, message: str) -> None:
    with new_session() as db:
        job = db.get(Job, job_id)
        job.warnings_json = json.dumps(job.warnings + [message])
        job.updated_at = utcnow()
        db.commit()
    log.warning(message, extra={"job_id": job_id})


def finish_job(job_id: str, *, files: list[str] | None = None, error: AppError | None = None) -> None:
    with new_session() as db:
        job = db.get(Job, job_id)
        now = utcnow()
        job.finished_at = now
        job.updated_at = now
        if error is None:
            job.status = JobStatus.SUCCEEDED
            job.progress = 100
            job.stage = "done"
            job.result_files_json = json.dumps(files or [])
        else:
            job.status = JobStatus.FAILED
            job.stage = "failed"
            job.error_json = json.dumps(error.to_dict()["error"])
        if job.parent_job_id:  # a clip swap ended (either way): the next swap of this video may start
            release_swap_lock(db, job.parent_job_id, job_id)
        db.commit()
    log.info("job finished", extra={"job_id": job_id,
                                     "fields": {"status": "failed" if error else "succeeded"}})


# ---------- Turning a job into JSON ----------

def file_link(base_url: str, job_id: str, filename: str) -> str:
    expires, sig = sign_file_link(job_id, filename)
    return f"{base_url.rstrip('/')}/api/v1/jobs/{job_id}/files/{filename}?expires={expires}&sig={sig}"


def job_to_dict(job: Job, base_url: str) -> dict[str, Any]:
    result = None
    if job.status == JobStatus.SUCCEEDED:
        result = {"files": {name: file_link(base_url, job.id, name) for name in job.result_files}}
    return {
        "job_id": job.id,
        "status": job.status,
        "progress": job.progress,
        "stage": job.stage,
        "input_type": job.input_type,
        "parent_job_id": job.parent_job_id,
        "warnings": job.warnings,
        "error": job.error,
        "result": result,
        "created_at": iso(job.created_at),
        "started_at": iso(job.started_at),
        "finished_at": iso(job.finished_at),
        "links": {"self": f"{base_url.rstrip('/')}/api/v1/jobs/{job.id}"},
    }


# ---------- Signed callback ----------

def callback_payload(job: Job) -> dict[str, Any]:
    base = get_settings().PUBLIC_BASE_URL or "http://localhost:8000"
    return {
        "event": "job.finished",
        "job_id": job.id,
        "status": job.status,
        "progress": job.progress,
        "error": job.error,
        "warnings": job.warnings,
        # Download links expire, so the receiver GETs the job for fresh ones.
        "job_url": f"{base.rstrip('/')}/api/v1/jobs/{job.id}",
    }


def send_callback(job_id: str, sleep=time.sleep) -> bool:
    """POST the result to callback_url, signed with HMAC. Retries with growing waits.

    The receiver checks: hex(HMAC-SHA256(raw body, CALLBACK_SECRET)) == X-IncuBrix-Signature.
    A failed callback never changes the job result; it is only recorded.
    """
    s = get_settings()
    with new_session() as db:
        job = db.get(Job, job_id)
        if not job.callback_url:
            return False
        url = job.callback_url
        body = json.dumps(callback_payload(job), sort_keys=True, separators=(",", ":")).encode()

    headers = {"Content-Type": "application/json", SIGNATURE_HEADER: hmac_sha256_hex(s.CALLBACK_SECRET, body)}
    attempts, delivered = 0, False
    for attempt in range(s.CALLBACK_MAX_RETRIES + 1):  # first try + N retries
        attempts += 1
        try:
            resp = httpx.post(url, content=body, headers=headers, timeout=s.CALLBACK_TIMEOUT_SECONDS)
            if 200 <= resp.status_code < 300:
                delivered = True
                break
            log.warning("callback got HTTP %s", resp.status_code, extra={"job_id": job_id})
        except httpx.HTTPError as e:
            log.warning("callback failed: %s", type(e).__name__, extra={"job_id": job_id})
        if attempt < s.CALLBACK_MAX_RETRIES:
            sleep(s.CALLBACK_BACKOFF_SECONDS * (2 ** attempt))  # 1 s, 2 s, 4 s ...

    with new_session() as db:
        job = db.get(Job, job_id)
        job.callback_status = "delivered" if delivered else "failed"
        job.callback_attempts = attempts
        db.commit()
    log.info("callback %s after %d attempt(s)", "delivered" if delivered else "failed", attempts,
             extra={"job_id": job_id})
    return delivered
