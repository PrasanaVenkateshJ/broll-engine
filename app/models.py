"""Database tables.

Job            = one video request and its progress.
IdempotencyKey = remembers "this client already sent this key with this body -> this job".
"""
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobStatus:
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), default=JobStatus.QUEUED, index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    stage: Mapped[str] = mapped_column(String(32), default="queued")
    input_type: Mapped[str] = mapped_column(String(16))  # audio_url | upload | script | scenes | swap
    # Clip swap jobs only: the ORIGINAL B-roll job whose video they change (None for normal jobs).
    parent_job_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # Original jobs only: the swap job that currently holds this video's "one swap at a time" lock
    # (None = free). Taken and released with conditional UPDATEs in jobs.py, so it is atomic.
    active_swap_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    request_json: Mapped[str] = mapped_column(Text)  # the validated request
    input_key: Mapped[str | None] = mapped_column(String(255), nullable=True)  # storage key of an upload
    callback_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    callback_status: Mapped[str | None] = mapped_column(String(16), nullable=True)  # delivered | failed
    callback_attempts: Mapped[int] = mapped_column(Integer, default=0)
    warnings_json: Mapped[str] = mapped_column(Text, default="[]")
    error_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_files_json: Mapped[str] = mapped_column(Text, default="[]")  # list of output filenames
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # How often this job was put back in the queue because the server stopped while it ran (see
    # jobs.recover_interrupted_jobs). After one re-queue it fails with JOB_INTERRUPTED instead.
    requeue_count: Mapped[int] = mapped_column(Integer, default=0)
    # X-Correlation-Id of the request that created the job; the worker logs it too.
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Small helpers: JSON text columns <-> Python objects.
    @property
    def request(self) -> dict[str, Any]:
        return json.loads(self.request_json)

    @property
    def warnings(self) -> list[str]:
        return json.loads(self.warnings_json or "[]")

    @property
    def error(self) -> dict[str, Any] | None:
        return json.loads(self.error_json) if self.error_json else None

    @property
    def result_files(self) -> list[str]:
        return json.loads(self.result_files_json or "[]")


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    # The key is unique PER CLIENT, so two users can pick the same key safely.
    client_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    body_hash: Mapped[str] = mapped_column(String(64))
    job_id: Mapped[str] = mapped_column(String(32), ForeignKey("jobs.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
