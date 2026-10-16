# syntax=docker/dockerfile:1
# Hugging Face Docker Space (CPU basic: 2 vCPU, 16 GB RAM). ONE container runs MinIO + worker + API.
#
# The app code is NOT in this folder: it is cloned from the private GitHub repo at build time with
# the GH_TOKEN build secret (Space settings -> Variables and secrets). A build secret is mounted
# only for that one RUN step, as a file in /run/secrets/: it is never stored in an image layer,
# never in an env var of the image and never printed in the build log.

# Stage 1: the same MinIO image docker-compose.yml uses, only to copy its server program from it.
FROM pgsty/minio:RELEASE.2026-08-04T00-00-00Z AS minio

# Stage 2: the real image.
FROM python:3.11-slim

# Space "Variables" arrive as build args, so these two can be changed without editing this file.
ARG GITHUB_REPO=PrasanaVenkateshJ/broll-engine
ARG GIT_BRANCH=main

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg = all audio/video work; espeak-ng = Kokoro pronunciation helper; fonts-dejavu-core = caption
# font; git = clone the code; curl = health checks in start.sh.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg espeak-ng fonts-dejavu-core git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# MinIO server (one static program). Used ONLY as the internal storage service (AGPL, unchanged).
COPY --from=minio /usr/bin/minio /usr/local/bin/minio

# Hugging Face runs the container as user id 1000, so everything the app writes must belong to it.
RUN useradd -m -u 1000 user

# torch + torchvision as the CPU pair pinned in requirements-ml.txt (before the clone, so a new
# commit does not re-download ~800 MB of torch when the build cache is still there).
RUN for attempt in 1 2 3; do \
        pip install --retries 5 --timeout 60 --index-url https://download.pytorch.org/whl/cpu \
            torch==2.14.1 torchvision==0.29.1 && break; \
        if [ "$attempt" = 3 ]; then exit 1; fi; \
        echo "torch download failed (attempt $attempt), retrying in 15 s"; sleep 15; \
    done

# REVISION changes on every upload by tools/deploy_hf.py, so the clone below is never an old cached copy.
COPY REVISION /tmp/REVISION

# Clone with the token sent as an HTTP header for this one command (-c ...): the token is not in the
# clone URL, so it is not saved in .git/config, and .git is deleted afterwards anyway.
RUN --mount=type=secret,id=GH_TOKEN,mode=0444,required=true \
    set -eu; \
    token="$(tr -d '\r\n ' < /run/secrets/GH_TOKEN)"; \
    basic="$(printf 'x-access-token:%s' "$token" | base64 | tr -d '\n')"; \
    git -c "http.extraHeader=Authorization: Basic $basic" clone --quiet --depth 1 --branch "$GIT_BRANCH" \
        "https://github.com/$GITHUB_REPO.git" /home/user/app; \
    unset token basic; \
    commit="$(git -C /home/user/app rev-parse HEAD)"; \
    echo "$commit" > /home/user/app/BUILD_COMMIT; \
    echo "built from $GITHUB_REPO@$GIT_BRANCH commit $commit (upload: $(cat /tmp/REVISION))"; \
    rm -rf /home/user/app/.git; \
    chown -R user:user /home/user/app

WORKDIR /home/user/app
RUN pip install --retries 5 --timeout 60 -r requirements-ml.txt \
    && pip install --retries 5 --timeout 60 -r requirements.txt

USER user
ENV HOME=/home/user \
    PYTHONPATH=/home/user/app \
    # Model files live in the image (downloaded once, below), so a cold start downloads nothing.
    HF_HOME=/home/user/models/huggingface \
    TORCH_HOME=/home/user/models/torch \
    XDG_CACHE_HOME=/home/user/models/cache

COPY --chown=user:user preload_models.py start.sh /home/user/
# Whisper "small", Kokoro (model + every voice in TTS_VOICE_MAP), open_clip ViT-B-32, spaCy en_core_web_sm.
RUN python /home/user/preload_models.py

EXPOSE 7860
CMD ["bash", "/home/user/start.sh"]
