#!/usr/bin/env bash
# Starts everything inside the ONE Space container:
#   1) MinIO (S3 storage, only reachable inside the container)  2) the bucket  3) the worker  4) the API on :7860
# Secrets come from the Space settings (Variables and secrets); they arrive here as env vars.
# Every value below uses ${NAME:-default}: a Space variable with the same name overrides it.
set -euo pipefail
cd /home/user/app
started=$SECONDS

fail() { echo "FATAL: $*" >&2; exit 1; }

# ---------- Secrets that MUST be set (auth is always ON in the live demo) ----------
for name in API_TOKENS FILE_LINK_SECRET CALLBACK_SECRET; do
  [ -n "${!name:-}" ] || fail "Space secret $name is not set (Settings -> Variables and secrets)."
done
case "$FILE_LINK_SECRET$CALLBACK_SECRET" in *change-me*) fail "FILE_LINK_SECRET / CALLBACK_SECRET still hold the example value.";; esac
for name in PIXABAY_API_KEY GROQ_API_KEY; do
  [ -n "${!name:-}" ] || echo "WARNING: $name is not set: that provider is skipped (Groq falls back to rules)." >&2
done
unset GH_TOKEN  # build-only secret (clone); the running app never needs it

# ---------- Temporary storage: everything here is lost when the Space restarts ----------
DATA=/tmp/broll
mkdir -p "$DATA/work" /tmp/minio
random_hex() { python -c "import secrets; print(secrets.token_hex($1))"; }

# MinIO login: random for every start unless set (nobody outside the container can reach MinIO anyway).
export S3_ACCESS_KEY="${S3_ACCESS_KEY:-broll$(random_hex 6)}"
export S3_SECRET_KEY="${S3_SECRET_KEY:-$(random_hex 24)}"
export STORAGE_BACKEND=s3
export S3_ENDPOINT_URL=http://127.0.0.1:9000
export S3_BUCKET="${S3_BUCKET:-broll}"
export S3_REGION="${S3_REGION:-us-east-1}"
export DATABASE_URL="sqlite:///$DATA/broll.db"
export WORK_DIR="$DATA/work"
export LOCAL_STORAGE_DIR="$DATA/storage"
export FOOTAGE_CACHE_DIR="$DATA/footage_cache"
export PROVIDERS_OVERRIDE_FILE="$DATA/providers_override.json"

# ---------- App settings for the live demo ----------
# SPACE_HOST is set by Hugging Face (e.g. "name-broll-studio.hf.space"): used for links in callbacks.
export PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-${SPACE_HOST:+https://$SPACE_HOST}}"
export LOG_LEVEL="${LOG_LEVEL:-INFO}"
export TTS_PROVIDER="${TTS_PROVIDER:-kokoro}"
export TRANSCRIBE_PROVIDER="${TRANSCRIBE_PROVIDER:-whisper}"
export WHISPER_MODEL="${WHISPER_MODEL:-small}"
export UNDERSTAND_PROVIDER="${UNDERSTAND_PROVIDER:-groq}"
export PROVIDER_ORDER="${PROVIDER_ORDER:-pixabay_video,pixabay_image_motion,wikimedia,generated}"
export RANK_SCORER="${RANK_SCORER:-clip}"
export WIKIMEDIA_USER_AGENT="${WIKIMEDIA_USER_AGENT:-broll-engine/0.3 (https://github.com/PrasanaVenkateshJ/broll-engine)}"
# 2 vCPU: one job at a time, 2 scenes searched + scored in parallel, 2 threads per model.
export WORKER_CONCURRENCY="${WORKER_CONCURRENCY:-1}"
export FOOTAGE_WORKERS="${FOOTAGE_WORKERS:-2}"
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-2}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-2}"
export RATE_LIMIT_POST_PER_MINUTE="${RATE_LIMIT_POST_PER_MINUTE:-10}"
export RATE_LIMIT_GET_PER_MINUTE="${RATE_LIMIT_GET_PER_MINUTE:-600}"
# The Space page (huggingface.co/spaces/...) shows the app in a frame: allow exactly that site.
export STUDIO_FRAME_ANCESTORS="${STUDIO_FRAME_ANCESTORS:-https://huggingface.co}"
export ALLOW_PRIVATE_URLS=false  # never overridable: user URLs must not reach MinIO or other internal addresses
[ "${RATE_LIMIT_POST_PER_MINUTE}" != 0 ] && [ "${RATE_LIMIT_GET_PER_MINUTE}" != 0 ] \
  || fail "rate limits must stay on in the live demo (RATE_LIMIT_*_PER_MINUTE > 0)."

echo "starting B-roll Studio, commit $(cat BUILD_COMMIT 2>/dev/null || echo unknown)"

# ---------- 1) MinIO, 2) bucket ----------
MINIO_ROOT_USER="$S3_ACCESS_KEY" MINIO_ROOT_PASSWORD="$S3_SECRET_KEY" MINIO_BROWSER=off \
  minio server /tmp/minio --address 127.0.0.1:9000 --quiet &
minio_pid=$!
python scripts/create_bucket.py || fail "MinIO did not start."

# ---------- 3) worker, 4) API ----------
python -m app.worker &
worker_pid=$!
uvicorn app.main:app --host 0.0.0.0 --port 7860 --proxy-headers --forwarded-allow-ips='*' &
api_pid=$!

# One clear line once /ready answers (the cold start time is counted from the container start).
(
  until curl -fsS -o /dev/null http://127.0.0.1:7860/ready; do sleep 1; done
  echo "===== B-roll Studio READY on port 7860 (${PUBLIC_BASE_URL:-local}) after $((SECONDS - started)) s ====="
) &

# If MinIO, the worker or the API stops, stop the container so Hugging Face restarts it.
trap 'kill $(jobs -p) 2>/dev/null; exit 0' TERM INT
set +e
wait -n "$minio_pid" "$worker_pid" "$api_pid"  # only these three (not the READY line above)
echo "FATAL: a process stopped (exit code $?); stopping the container." >&2
kill $(jobs -p) 2>/dev/null
exit 1
