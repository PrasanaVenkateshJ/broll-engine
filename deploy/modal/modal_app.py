"""The live demo on Modal (modal.com): ONE container runs the API (FastAPI) AND the job worker (a thread).

Deploy (from the project folder, PowerShell):   .venv\\Scripts\\modal deploy deploy/modal/modal_app.py

Why it looks like this (the free plan here is about $1 of credit a month, billed per second while a
container runs - see testpack/notes/cost_modal.md):
- min_containers=0: no container (and no cost) while nobody uses the demo. The first request after a
  quiet time waits for a cold start.
- max_containers=1: never two containers, so ONE process owns the SQLite file (SQLite has no locking
  across machines) and the daily / monthly job caps are counted in one place.
- scaledown_window (180 s): an idle container stays up this long after the LAST request. Every visit costs
  at least this much idle time, so it is the biggest cost knob. While a job runs, its client (Studio,
  batch runner, live_check) keeps polling, which keeps the container up. If a container still stops
  mid-job, the next one re-queues that job once (recover_interrupted_jobs in start()).
- @modal.concurrent: one container answers many requests at once (polling, downloads, the Studio page)
  while the worker thread renders.
- Data on the Modal Volume "broll-data" (/data): SQLite database + output files (STORAGE_BACKEND=local).
  docker compose uses MinIO (S3) instead; the API and links behave the same.
- Secrets come ONLY from the Modal secret "broll-secrets" (never from this repo or the image).

Deploy-time knobs (read from YOUR PowerShell when you run "modal deploy"; unset = the default below):
  MODAL_CPU, MODAL_MEMORY_MB, MODAL_MEMORY_LIMIT_MB, MODAL_SCALEDOWN_SECONDS,
  DEMO_MAX_JOBS_PER_DAY, DEMO_MAX_JOBS_PER_MONTH, DEMO_MAX_AUDIO_SECONDS, DEMO_RATE_LIMIT_POST_PER_MINUTE,
  DEMO_RATE_LIMIT_GET_PER_MINUTE, MODAL_PUBLIC_URL
"""
import logging
import os
import threading
import time
from pathlib import Path

import modal

APP_NAME = "broll-studio"  # also the URL label: https://<workspace>--broll-studio.modal.run
DATA = "/data"             # where the Volume is mounted
CODE = "/app"              # where our code lives in the image

# Locally (deploy) the repo is two folders up from this file; inside the container the code is in /app.
REPO = Path(__file__).resolve().parents[2] if modal.is_local() else Path(CODE)


def knob(name: str, default: str) -> str:
    return os.environ.get(name) or default


CPU = float(knob("MODAL_CPU", "2"))  # physical cores (Modal bills per core-second)
# Memory REQUEST (MiB, billed even when idle) and hard LIMIT (MiB, the container is stopped above it).
# Modal bills max(request, real use), so a request near the IDLE use is cheapest; the limit only guards
# against a runaway job. Measured locally (testpack/notes/cost_modal.md): the worker peaked at 4,264 MB in run 1
# and was above 3.5 GB in hundreds of 1-second samples, so the old 3 GiB request was often exceeded (Modal then
# bills the real use anyway). 4.5 GiB covers that peak with ~10% room; the 7 GiB limit only stops a runaway job.
MEMORY_MB = int(knob("MODAL_MEMORY_MB", "4608"))
MEMORY_LIMIT_MB = int(knob("MODAL_MEMORY_LIMIT_MB", "7168"))
SCALEDOWN_SECONDS = int(knob("MODAL_SCALEDOWN_SECONDS", "180"))

# The models, in ONE place: the image downloads exactly these and the app is told to use exactly these.
MODELS = {
    "WHISPER_MODEL": "small",
    "KOKORO_REPO_ID": "hexgrad/Kokoro-82M",
    "CLIP_MODEL": "ViT-B-32",
    "CLIP_PRETRAINED": "laion2b_s34b_b79k",
}
MODEL_CACHE = {
    "HF_HOME": "/models/huggingface",
    "TORCH_HOME": "/models/torch",
    "XDG_CACHE_HOME": "/models/cache",
}
# torch + torchvision CPU pair; must equal the pins in requirements-ml.txt (a test checks it).
TORCH_PINS = ["torch==2.14.1", "torchvision==0.29.1"]

# Never copied into the image: secrets, local data, the virtualenv, big test media, personal files.
IGNORE = [".git", ".venv", ".env", "**/.env", "data", "**/__pycache__", "**/*.pyc", ".pytest_cache",
          "testpack", "*.xlsx", "tools/bin", ".claude"]

image = (
    modal.Image.debian_slim(python_version="3.11")
    # ffmpeg = all audio/video work; espeak-ng = Kokoro pronunciation helper; fonts-dejavu-core = captions
    # (Tamil / Devanagari caption fonts come with the code in app/fonts)
    .apt_install("ffmpeg", "espeak-ng", "fonts-dejavu-core")
    .pip_install(*TORCH_PINS, index_url="https://download.pytorch.org/whl/cpu")
    .pip_install_from_requirements(str(REPO / "requirements-ml.txt"))
    .pip_install_from_requirements(str(REPO / "requirements.txt"))
    .env(MODEL_CACHE)
    # Step 1 (cached): download the models BEFORE the code, so a code change does not re-download them.
    .add_local_file(REPO / "deploy" / "modal" / "download_models.py", "/build/download_models.py", copy=True)
    .run_commands("python /build/download_models.py", env=MODELS)
    # Step 2: our code, then load every model the way the app does WITHOUT network (proves nothing is missing).
    .add_local_dir(REPO, CODE, copy=True, ignore=IGNORE)
    .workdir(CODE)
    .env({"PYTHONPATH": CODE, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"})
    .run_commands(f"HF_HUB_OFFLINE=1 python {CODE}/deploy/hf-space/preload_models.py", env=MODELS)
    # At run time the models are only READ from the image: a cold start downloads nothing.
    .env({"HF_HUB_OFFLINE": "1"})
)

# Settings of the live demo (every name is a normal app setting, see app/config.py / README).
DEMO_ENV = {
    **MODELS,
    "LOG_LEVEL": "INFO",
    # Storage: SQLite + output files on the Volume; temp work + footage cache on the container's own disk.
    "STORAGE_BACKEND": "local",
    "DATABASE_URL": f"sqlite:///{DATA}/broll.db",
    "SQLITE_JOURNAL_MODE": "DELETE",  # no shared-memory WAL file on a Volume
    "LOCAL_STORAGE_DIR": f"{DATA}/storage",
    "PROVIDERS_OVERRIDE_FILE": f"{DATA}/providers_override.json",
    "WORK_DIR": "/tmp/broll/work",
    "FOOTAGE_CACHE_DIR": "/tmp/broll/footage_cache",
    # Real adapters.
    "TTS_PROVIDER": "kokoro",
    "TRANSCRIBE_PROVIDER": "whisper",
    "UNDERSTAND_PROVIDER": "groq",
    "RANK_SCORER": "clip",
    "PROVIDER_ORDER": "pixabay_video,pixabay_image_motion,wikimedia,openverse,generated",
    "TTS_CACHE_DIR": "/tmp/broll/tts_cache",
    # balanced: 15 candidates per source, early exit, judge (if VLM_PROVIDER is set) only for close calls.
    "QUALITY_MODE": knob("DEMO_QUALITY_MODE", "balanced"),
    # 2 cores: one ffmpeg segment at a time (each already uses both cores).
    "RENDER_WORKERS": "1",
    # One job at a time; 2 scenes searched + scored in parallel; 2 threads per model.
    "WORKER_CONCURRENCY": "1",
    "FOOTAGE_WORKERS": "2",
    "OMP_NUM_THREADS": "2",
    "MKL_NUM_THREADS": "2",
    # Cost guards (sized for about $1/month of credit, see testpack/notes/cost_modal.md).
    "DEMO_MAX_JOBS_PER_DAY": knob("DEMO_MAX_JOBS_PER_DAY", "12"),
    "DEMO_MAX_JOBS_PER_MONTH": knob("DEMO_MAX_JOBS_PER_MONTH", "35"),
    "MAX_AUDIO_SECONDS": knob("DEMO_MAX_AUDIO_SECONDS", "300"),
    "RATE_LIMIT_POST_PER_MINUTE": knob("DEMO_RATE_LIMIT_POST_PER_MINUTE", "4"),
    "RATE_LIMIT_GET_PER_MINUTE": knob("DEMO_RATE_LIMIT_GET_PER_MINUTE", "300"),
    # Safety: user URLs may never reach internal addresses.
    "ALLOW_PRIVATE_URLS": "false",
    "STUDIO_FRAME_ANCESTORS": "",
    # Links in callbacks. Empty = looked up when the container starts (see own_web_url).
    "PUBLIC_BASE_URL": knob("MODAL_PUBLIC_URL", ""),
}

# Secrets the demo refuses to start without (auth must always be ON in public).
REQUIRED_SECRETS = ("API_TOKENS", "FILE_LINK_SECRET", "CALLBACK_SECRET")

app = modal.App(APP_NAME)
volume = modal.Volume.from_name("broll-data", create_if_missing=True)
log = logging.getLogger("modal_app")


def force_https(asgi_app):
    """Modal serves web endpoints ONLY over HTTPS, but the app may see the request as plain http behind
    Modal's proxy. Then links built from the request (signed downloads) would start with http:// and a
    browser would block them on the https page. So every request is marked as https."""
    async def wrapped(scope, receive, send):
        if scope["type"] == "http" and scope.get("scheme") == "http":
            scope = {**scope, "scheme": "https"}
        await asgi_app(scope, receive, send)

    return wrapped


def own_web_url() -> str:
    """This app's public URL (for links inside callbacks). Best effort: '' if Modal cannot tell us."""
    try:
        return modal.Cls.from_name(APP_NAME, "BrollStudio")().web.get_web_url() or ""
    except Exception as e:  # never block the start for this; callbacks then fall back to localhost links
        print(f"modal_app: could not look up own URL ({type(e).__name__}); set MODAL_PUBLIC_URL", flush=True)
        return ""


def commit_with_db_locked(vol, engine) -> None:
    """Persist the Volume now (database + output files), so a finished job survives a stop.

    BEGIN IMMEDIATE takes SQLite's write lock for the few seconds of the commit: nobody can be half-way
    through writing the database file while its snapshot is taken (writers just wait; busy_timeout is
    30 s). Modal also commits by itself every few seconds and at shutdown; this makes it happen NOW.
    """
    raw = engine.raw_connection()
    try:
        raw.cursor().execute("BEGIN IMMEDIATE")
        vol.commit()
    finally:
        raw.rollback()  # we changed nothing; this only releases the lock
        raw.close()


@app.cls(
    image=image,
    secrets=[modal.Secret.from_name("broll-secrets")],
    volumes={DATA: volume},
    env=DEMO_ENV,
    cpu=CPU,
    memory=(MEMORY_MB, MEMORY_LIMIT_MB),
    min_containers=0,
    max_containers=1,
    scaledown_window=SCALEDOWN_SECONDS,
    timeout=600,
    startup_timeout=300,
)
@modal.concurrent(max_inputs=32)
class BrollStudio:
    @modal.enter()
    def start(self):
        self.started = time.monotonic()
        missing = [n for n in REQUIRED_SECRETS if not os.environ.get(n) or "change-me" in os.environ[n]]
        if missing:
            raise RuntimeError(f"Modal secret 'broll-secrets' lacks a real value for: {', '.join(missing)}")
        if not os.environ.get("PUBLIC_BASE_URL"):
            os.environ["PUBLIC_BASE_URL"] = own_web_url()
        Path(DATA).mkdir(parents=True, exist_ok=True)

        # Imported here (not at the top): only the container has our code and its packages.
        from app.config import get_settings
        from app.db import init_db
        from app.jobs import recover_interrupted_jobs
        from app.logging_setup import setup_logging
        from app.pipeline.rank import ClipScorer
        from app.worker import start_workers

        get_settings.cache_clear()  # read the env set just above
        setup_logging(get_settings().LOG_LEVEL)
        init_db()
        self.save_lock = threading.Lock()
        # Jobs a previous container left "running" (it was stopped mid-job): queue once again, or fail them.
        recovered = recover_interrupted_jobs()
        if recovered["requeued"] or recovered["failed"]:
            self.save_volume()
        threading.Thread(target=ClipScorer.preload, name="clip-preload", daemon=True).start()
        self.stop = threading.Event()
        self.workers = start_workers(self.stop, after_job=lambda job_id: self.save_volume())
        log.info("modal container started", extra={"fields": {
            "public_base_url": os.environ["PUBLIC_BASE_URL"], "workers": len(self.workers)}})

    def save_volume(self) -> None:
        from app.db import get_engine

        with self.save_lock:  # the worker thread and shutdown may both call it
            commit_with_db_locked(volume, get_engine())

    @modal.asgi_app(label=APP_NAME)
    def web(self):
        from app.main import app as fastapi_app

        return force_https(fastapi_app)

    @modal.exit()
    def shutdown(self):
        # Modal gives about 30 s here. A job still rendering is NOT waited for: it stays "running" and the
        # next container re-queues it (recover_interrupted_jobs).
        self.stop.set()
        for t in self.workers:
            t.join(timeout=15)
        try:
            self.save_volume()
        except Exception:
            log.exception("volume commit at shutdown failed")
        # One line per container with its lifetime: the real input for the cost estimate.
        log.info("modal container stopped", extra={"fields": {
            "uptime_seconds": round(time.monotonic() - self.started, 1)}})
