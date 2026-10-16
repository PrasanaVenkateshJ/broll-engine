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
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app.config import get_settings
from app.db import check_db, get_db, init_db
from app.errors import AppError, ErrorCode, error_body, register_error_handlers, validation_details
from app.jobs import body_hash, create_job, find_existing_job, job_summary, job_to_dict, new_job_id
from app.logging_setup import correlation_id_var, setup_logging
from app.models import Job, JobStatus
from app.pipeline.intake import check_audio_file, check_scene_texts, check_script
from app.pipeline.swap import precheck as swap_precheck
from app.pipeline.swap import read_state
from app.pipeline.tts import check_voice
from app.schemas import (DEFAULT_VOICE, MAX_SCENES, MAX_SCRIPT_CHARS, SUPPORTED_ASPECT_RATIOS, JobCreate,
                         JobOptions, SwapRequest)
from app.security import BlockedURL, check_file_link, check_public_url, check_rate_limit, require_client
from app.storage import StorageNotFound, get_storage, input_key, output_key

log = logging.getLogger("app.main")

SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SAFE_CORRELATION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
# Studio files are one flat folder; only these names + types are ever served (no "../" tricks).
STUDIO_FILE = re.compile(r"^[A-Za-z0-9_-]{1,64}\.(html|css|js|svg|png|ico)$")
STUDIO_DEFAULT_DIR = Path(__file__).resolve().parent / "studio"
# Fixed types (Windows' mimetypes can say text/plain for .js, and browsers refuse such a module script).
STUDIO_TYPES = {"html": "text/html; charset=utf-8", "css": "text/css; charset=utf-8",
                "js": "text/javascript; charset=utf-8", "svg": "image/svg+xml", "png": "image/png",
                "ico": "image/x-icon"}


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
    try:
        # Look INSIDE the file now (ffprobe), so a wrong / broken / too long file gets a 4xx
        # straight away instead of a job that fails later.
        await run_in_threadpool(check_audio_file, tmp)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return options.model_dump(mode="json"), tmp, sha, ext


def _precheck_json_job(job_req: JobCreate) -> None:
    """Errors we can find without doing slow work -> answer 4xx now, before queuing."""
    if job_req.script is not None:
        check_script(job_req.script)
    if job_req.scenes is not None:
        check_scene_texts([sc.model_dump() for sc in job_req.scenes])
    if job_req.script is not None or job_req.scenes is not None:
        check_voice(job_req.voice)  # the voice only matters when we make the narration
    if job_req.audio_url is not None:
        try:  # SSRF guard: never fetch private / local addresses for a user
            check_public_url(str(job_req.audio_url))
        except BlockedURL as e:
            raise AppError(ErrorCode.INVALID_INPUT, f"audio_url is not allowed: {e}", 422,
                           details={"reason": "private_or_local_address"}) from None


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
        await run_in_threadpool(_precheck_json_job, job_req)  # may look up a host name
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


def _create_swap_job_sync(db: Session, client_id: str, idem_key: str, job_id: str, scene_index: int,
                          swap: SwapRequest) -> tuple[Job, bool]:
    """Checks + insert for a clip swap (runs in a thread: database + storage reads)."""
    payload = {"job_id": job_id, "scene_index": scene_index, "swap": swap.model_dump(mode="json")}
    req_hash = body_hash({"swap": payload})
    existing = find_existing_job(db, client_id, idem_key, req_hash)
    if existing is not None:  # same key + same body: the same swap job, no new work
        return existing, False
    job = db.get(Job, job_id)
    if job is None or job.client_id != client_id:
        raise AppError(ErrorCode.JOB_NOT_FOUND, f"Job {job_id} not found", 404)
    root = db.get(Job, job.parent_job_id) if job.parent_job_id else job  # a swap of a swap = same video
    if root.status in (JobStatus.QUEUED, JobStatus.RUNNING):
        raise AppError(ErrorCode.JOB_NOT_READY, f"Job {root.id} is still {root.status}; swap a clip after it "
                       "succeeds", 409, retryable=True, details={"status": root.status})
    if root.status == JobStatus.FAILED:
        raise AppError(ErrorCode.JOB_NOT_READY, f"Job {root.id} failed, so it has no video to change", 409,
                       details={"status": root.status})
    swap_precheck(read_state(root.id), scene_index, swap)
    payload["root_job_id"] = root.id
    # One swap at a time per video (each swap builds on the previous one). create_job() takes the
    # video's swap lock with ONE conditional UPDATE in the same transaction as the insert, so two
    # swaps sent at the same moment cannot both get in: the loser gets 409 SWAP_IN_PROGRESS.
    return create_job(db, job_id=new_job_id(), client_id=client_id, idem_key=idem_key, req_hash=req_hash,
                      input_type="swap", request_data=payload, parent_job_id=root.id)


@api.post("/broll/jobs/{job_id}/scenes/{scene_index}/swap", status_code=202, tags=["jobs"])
async def swap_scene_clip(
    job_id: str,
    scene_index: int,
    request: Request,
    client_id: str = Depends(require_client),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
):
    """Give one scene a different clip. Only that scene is re-rendered; the result is a new job
    (202 + job_id) whose files are the updated videos, timeline.json and manifest.json."""
    if not idempotency_key:
        raise AppError(ErrorCode.INVALID_INPUT, "The Idempotency-Key header is required for a clip swap", 422,
                       details={"header": "Idempotency-Key"})
    try:
        swap = SwapRequest.model_validate_json(await request.body() or b"{}")
    except ValidationError as e:
        raise _validation_error(e) from None
    if swap.clip_url is not None:
        try:  # SSRF guard, like audio_url
            await run_in_threadpool(check_public_url, str(swap.clip_url))
        except BlockedURL as e:
            raise AppError(ErrorCode.INVALID_INPUT, f"clip_url is not allowed: {e}", 422,
                           details={"reason": "private_or_local_address"}) from None
    job, created = await run_in_threadpool(_create_swap_job_sync, db, client_id, idempotency_key, job_id,
                                           scene_index, swap)
    links = {"self": f"{base_url(request).rstrip('/')}/api/v1/jobs/{job.id}",
             "parent": f"{base_url(request).rstrip('/')}/api/v1/jobs/{job.parent_job_id}"}
    headers = {"Location": links["self"]}
    if not created:
        headers["Idempotent-Replayed"] = "true"
    return JSONResponse(status_code=202, headers=headers,
                        content={"job_id": job.id, "status": job.status, "links": links})


@api.get("/jobs", tags=["jobs"])
def list_jobs(limit: int = Query(20), client_id: str = Depends(require_client), db: Session = Depends(get_db)):
    """The caller's OWN jobs (normal jobs and clip swaps), newest first."""
    max_limit = get_settings().JOBS_LIST_MAX
    if not 1 <= limit <= max_limit:
        raise AppError(ErrorCode.INVALID_INPUT, f"limit must be between 1 and {max_limit}", 422,
                       details={"min": 1, "max": max_limit})
    rows = db.scalars(select(Job).where(Job.client_id == client_id)
                      .order_by(Job.created_at.desc(), Job.id).limit(limit)).all()
    return {"jobs": [job_summary(job) for job in rows]}


@api.get("/config/public", tags=["config"])
def public_config():
    """Settings a user interface needs to build its form. Only safe values: no keys, secrets or paths."""
    s = get_settings()
    return {
        "app_version": s.APP_VERSION,
        "voices": list(s.tts_voices),
        "default_voice": DEFAULT_VOICE,
        "aspect_ratios": list(SUPPORTED_ASPECT_RATIOS),
        "max_audio_seconds": s.MAX_AUDIO_SECONDS,
        "max_upload_mb": s.MAX_UPLOAD_MB,
        "allowed_audio_extensions": sorted(s.allowed_audio_extensions),
        "max_script_chars": MAX_SCRIPT_CHARS,
        "max_scenes": MAX_SCENES,
    }


@api.get("/jobs/{job_id}", tags=["jobs"])
def get_job(job_id: str, request: Request, client_id: str = Depends(require_client), db: Session = Depends(get_db)):
    """Status, progress 0-100, stage, warnings, error and (when done) signed file links."""
    job = db.get(Job, job_id)
    if job is None or job.client_id != client_id:  # other clients' jobs look "not found"
        raise AppError(ErrorCode.JOB_NOT_FOUND, f"Job {job_id} not found", 404)
    return job_to_dict(job, base_url(request))


app.include_router(api)


# ---------- B-roll Studio (web UI; not part of the API contract) ----------

def _studio_file(filename: str) -> FileResponse:
    s = get_settings()
    folder = Path(s.STUDIO_DIR) if s.STUDIO_DIR else STUDIO_DEFAULT_DIR
    path = folder / filename
    if not s.STUDIO_ENABLED or not STUDIO_FILE.match(filename) or not path.is_file():
        raise AppError(ErrorCode.NOT_FOUND, "File not found", 404)
    headers = {
        "Cache-Control": "no-cache",  # the browser re-checks (ETag) so a changed file shows at once
        "X-Content-Type-Options": "nosniff",
        # Signed download links are in the page's URLs: never send them to other sites as a Referer.
        "Referrer-Policy": "no-referrer",
        # Only our own scripts / styles; videos + thumbnails come from our API (or a blob made from it).
        "Content-Security-Policy": "default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; "
                                   "object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
    }
    return FileResponse(path, headers=headers, media_type=STUDIO_TYPES[filename.rsplit(".", 1)[1]])


@app.get("/", include_in_schema=False)
def studio_page():
    """The B-roll Studio web page (plain HTML + CSS + JavaScript, no build step)."""
    return _studio_file("index.html")


@app.get("/studio/{filename}", include_in_schema=False)
def studio_static(filename: str):
    return _studio_file(filename)


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
    check_rate_limit(job.client_id, "get")  # no token here: downloads count for the job's owner
    if job.status != JobStatus.SUCCEEDED or filename not in job.result_files:
        raise AppError(ErrorCode.NOT_FOUND, "File not found", 404)
    try:
        chunks, size = get_storage().open_stream(output_key(job_id, filename))
    except StorageNotFound:
        raise AppError(ErrorCode.NOT_FOUND, "File not found in storage", 404) from None
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return StreamingResponse(chunks, media_type=media_type, headers={
        "Content-Length": str(size), "Content-Disposition": f'inline; filename="{filename}"'})
