# syntax=docker/dockerfile:1
# One image for api, worker and minio-init (they only differ in the start command).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Model downloads (whisper, kokoro, open_clip) go here -> a Docker volume, so they
    # are downloaded once and survive container restarts.
    HF_HOME=/models/huggingface \
    TORCH_HOME=/models/torch \
    XDG_CACHE_HOME=/models/cache

# ffmpeg = all audio/video work; espeak-ng = pronunciation helper used by Kokoro TTS;
# fonts-dejavu-core = a font for the (Latin) captions burned into the videos. Tamil / Devanagari fonts
# (Noto, SIL OFL) come with the code in app/fonts.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg espeak-ng fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies BEFORE copying the code, so a code change does not
# re-download torch (Docker re-uses this cached layer).
COPY requirements.txt requirements-ml.txt ./
# 1) torch + torchvision TOGETHER from the CPU-only index, as a matching pair
#    (torch 2.14.1 <-> torchvision 0.29.1). That server once answered "409 Conflict" in CI,
#    so this step is tried up to 3 times.
# 2) Everything else from PyPI only. torch/torchvision are already there with the pinned
#    versions, so open_clip cannot pull a different (CUDA, multi-GB) torchvision.
# --retries 5 --timeout 60: retry each download 5 times, wait up to 60 s for an answer.
# --mount=type=cache keeps pip's download cache between builds on the same machine.
RUN --mount=type=cache,target=/root/.cache/pip \
    for attempt in 1 2 3; do \
        pip install --retries 5 --timeout 60 --index-url https://download.pytorch.org/whl/cpu torch==2.14.1 torchvision==0.29.1 && break; \
        if [ "$attempt" = 3 ]; then exit 1; fi; \
        echo "torch download failed (attempt $attempt), retrying in 15 s"; sleep 15; \
    done \
    && pip install --retries 5 --timeout 60 -r requirements-ml.txt \
    && pip install --retries 5 --timeout 60 -r requirements.txt

COPY . .
RUN mkdir -p /app/data /models

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
