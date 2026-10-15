"""The FastAPI app: middleware, health checks and the /api/v1 routes.

Run it with:  uvicorn app.main:app --port 8000
"""
import hashlib
import logging
import mimetypes
import os
import re
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app.config import get_settings
from app.db import check_db, get_db, init_db
from app.errors import AppError, ErrorCode, error_body, register_error_handlers, validation_details
from app.jobs import body_hash, create_job, find_existing_job, job_to_dict, new_job_id
from app.logging_setup import correlation_id_var, setup_logging
from app.models import Job, JobStatus
from app.schemas import JobCreate, JobOptions
from app.security import check_file_link, require_client
from app.storage import StorageNotFound, get_storage, input_key, output_key

log = logging.getLogger("app.main")

SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SAFE_CORRELATION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    setup_logging(s.LOG_LEVEL)
    init_db()
    if not s.api_tokens:
        log.warning("API_TOKENS is empty: authentication is OFF (ok for local dev only)")
    log.info("api started", extra={"fields": {"storage": s.STORAGE_BACKEND, "version": s.APP_VERSION}})
    yield


app = FastAPI(title="IncuBrix B-roll Engine", version=get_settings().APP_VERSION, lifespan=lifespan)
register_error_handlers(app)


@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    """Read X-Correlation-Id (or make one), put it in every log line, echo it back."""
    incoming = request.headers.get("X-Correlation-Id", "")
    cid = incoming if SAFE_CORRELATION_ID.match(incoming) else uuid.uuid4().hex
    correlation_id_var.set(cid)
    start = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Correlation-Id"] = cid
    log.info("request", extra={"fields": {
        "method": request.method, "path": request.url.path, "status": response.status_code,
        "duration_ms": round((time.perf_counter() - start) * 1000, 1)}})
    return response


def base_url(request: Request) -> str:
    return get_settings().PUBLIC_BASE_URL or str(request.base_url)


# ---------- Health ----------

@app.get("/health", tags=["health"])
def health():
    """Process is alive. Does not check anything else."""
    return {"status": "ok"}


@app.get("/ready", tags=["health"])
def ready():
    """Ready to take work = database AND the active storage backend both answer."""
    s = get_settings()
    checks = {"database": "ok", "storage": "ok", "storage_backend": s.STORAGE_BACKEND}
    try:
        check_db()
    except Exception as e:
        checks["database"] = f"error: {type(e).__name__}"
    try:
        get_storage().check()
    except Exception as e:
        checks["storage"] = f"error: {type(e).__name__}"
    if checks["database"] != "ok" or checks["storage"] != "ok":
        return JSONResponse(status_code=503, content=error_body(ErrorCode.NOT_READY, "Service not ready", True, checks))
    return {"status": "ready", "checks": checks}


# ---------- /api/v1 (needs a bearer token) ----------

api = APIRouter(prefix="/api/v1", dependencies=[Depends(require_client)])


def _validation_error(exc: ValidationError) -> AppError:
    errors = exc.errors()
    if any("unsupported aspect ratio" in str(e.get("msg", "")) for e in errors):
        return AppError(ErrorCode.UNSUPPORTED_ASPECT_RATIO, "Aspect ratio must be 16:9, 9:16 or 1:1", 422,
                        details=validation_details(errors))
    return AppError(ErrorCode.INVALID_INPUT, "Request validation failed", 422, details=validation_details(errors))


async def _save_upload(upload: UploadFile, max_bytes: int) -> tuple[Path, str]:
    """Copy the uploaded file to a temp file in 1 MB pieces. Returns (path, sha256)."""
    work = Path(get_settings().WORK_DIR)
    work.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=work, suffix=".upload")
    sha, size = hashlib.sha256(), 0
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise AppError(ErrorCode.FILE_TOO_LARGE, f"Upload is larger than {max_bytes // (1024 * 1024)} MB",
                                   413, details={"max_bytes": max_bytes})
                sha.update(chunk)
                out.write(chunk)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    if size == 0:
        Path(tmp).unlink(missing_ok=True)
        raise AppError(ErrorCode.INVALID_INPUT, "Uploaded file is empty", 422)
    return Path(tmp), sha.hexdigest()


async def _read_multipart(request: Request) -> tuple[dict, Path, str, str]:
    """Multipart: field "audio" (the file) + optional field "options" (JSON text).

    Returns (validated options, temp file path, file sha256, file extension).
    """
    s = get_settings()
    max_bytes = s.MAX_UPLOAD_MB * 1024 * 1024
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes + 1024 * 1024:
        raise AppError(ErrorCode.FILE_TOO_LARGE, f"Upload is larger than {s.MAX_UPLOAD_MB} MB", 413,
                       details={"max_bytes": max_bytes})

    form = await request.form(max_files=1, max_fields=5)
    upload = form.get("audio")
    if not isinstance(upload, UploadFile):
        raise AppError(ErrorCode.INVALID_INPUT, "Multipart request needs a file in field 'audio'", 422)
    ext = Path(upload.filename or "").suffix.lower().lstrip(".")
    if ext not in s.allowed_audio_extensions:
        raise AppError(ErrorCode.UNSUPPORTED_FILE_TYPE, f"File type '.{ext}' is not supported",
                       415, details={"allowed": sorted(s.allowed_audio_extensions)})

    raw_options = form.get("options") or "{}"
    if not isinstance(raw_options, str):
        raise AppError(ErrorCode.INVALID_INPUT, "Field 'options' must be JSON text, not a file", 422)
    try:
        options = JobOptions.model_validate_json(raw_options)
    except ValidationError as e:
        raise _validation_error(e) from None

    tmp, sha = await _save_upload(upload, max_bytes)
    return options.model_dump(mode="json"), tmp, sha, ext


def _create_job_sync(db: Session, client_id: str, idem_key: str | None, req_hash: str, input_type: str,
                     payload: dict, upload: tuple[Path, str] | None) -> tuple[Job, bool]:
    """Runs in a thread (database + file copy are blocking work)."""
    try:
        existing = find_existing_job(db, client_id, idem_key, req_hash)
        if existing is not None:
            return existing, False
        job_id = new_job_id()
        stored_key = None
        if upload:
            tmp, ext = upload
            stored_key = input_key(job_id, f"audio.{ext}")
            get_storage().put_file(stored_key, tmp)  # save the input BEFORE the job becomes visible
        job, created = create_job(db, job_id=job_id, client_id=client_id, idem_key=idem_key, req_hash=req_hash,
                                  input_type=input_type, request_data=payload, input_key=stored_key)
        if not created and stored_key:
            get_storage().delete(stored_key)  # lost an idempotency race: drop our copy
        return job, created
    finally:
        if upload:
            upload[0].unlink(missing_ok=True)


@api.post("/broll/jobs", status_code=202, tags=["jobs"])
async def create_broll_job(
    request: Request,
    client_id: str = Depends(require_client),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
):
    """Start a B-roll job. Send JSON (audio_url | script | scenes) OR multipart (audio + options)."""
    content_type = request.headers.get("content-type", "").lower()
    upload = None
    if content_type.startswith("multipart/form-data"):
        payload, tmp, file_sha, ext = await _read_multipart(request)
        input_type, upload = "upload", (tmp, ext)
        req_hash = body_hash(payload, file_sha)
    elif content_type.startswith("application/json") or not content_type:
        try:
            job_req = JobCreate.model_validate_json(await request.body() or b"{}")
        except ValidationError as e:
            raise _validation_error(e) from None
        payload = job_req.model_dump(mode="json")
        input_type = job_req.input_type
        req_hash = body_hash(payload)
    else:
        raise AppError(ErrorCode.INVALID_INPUT, "Content-Type must be application/json or multipart/form-data", 415)

    job, created = await run_in_threadpool(
        _create_job_sync, db, client_id, idempotency_key, req_hash, input_type, payload, upload)

    links = {"self": f"{base_url(request).rstrip('/')}/api/v1/jobs/{job.id}"}
    headers = {"Location": links["self"]}
    if not created:
        headers["Idempotent-Replayed"] = "true"
    return JSONResponse(status_code=202, headers=headers,
                        content={"job_id": job.id, "status": job.status, "links": links})


@api.get("/jobs/{job_id}", tags=["jobs"])
def get_job(job_id: str, request: Request, client_id: str = Depends(require_client), db: Session = Depends(get_db)):
    """Status, progress 0-100, stage, warnings, error and (when done) signed file links."""
    job = db.get(Job, job_id)
    if job is None or job.client_id != client_id:  # other clients' jobs look "not found"
        raise AppError(ErrorCode.JOB_NOT_FOUND, f"Job {job_id} not found", 404)
    return job_to_dict(job, base_url(request))


app.include_router(api)


# ---------- Signed file downloads (no bearer token: the signature IS the permission) ----------

@app.get("/api/v1/jobs/{job_id}/files/{filename}", tags=["jobs"])
def download_file(job_id: str, filename: str, expires: int = Query(...), sig: str = Query(..., max_length=128),
                  db: Session = Depends(get_db)):
    """Stream one output file. The link (from GET /jobs/{id}) carries an HMAC signature + expiry."""
    if not SAFE_FILENAME.match(filename):
        raise AppError(ErrorCode.NOT_FOUND, "File not found", 404)
    check_file_link(job_id, filename, expires, sig)
    job = db.get(Job, job_id)
    if job is None:
        raise AppError(ErrorCode.JOB_NOT_FOUND, f"Job {job_id} not found", 404)
    if job.status != JobStatus.SUCCEEDED or filename not in job.result_files:
        raise AppError(ErrorCode.NOT_FOUND, "File not found", 404)
    try:
        chunks, size = get_storage().open_stream(output_key(job_id, filename))
    except StorageNotFound:
        raise AppError(ErrorCode.NOT_FOUND, "File not found in storage", 404) from None
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return StreamingResponse(chunks, media_type=media_type, headers={
        "Content-Length": str(size), "Content-Disposition": f'inline; filename="{filename}"'})
