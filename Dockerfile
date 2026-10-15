# One image for both the api and the worker (they only differ in the start command).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Model downloads (whisper, kokoro, open_clip) go here -> a Docker volume, so they
    # are downloaded once and survive container restarts.
    HF_HOME=/models/huggingface \
    TORCH_HOME=/models/torch \
    XDG_CACHE_HOME=/models/cache

# ffmpeg = all audio/video work; espeak-ng = pronunciation helper used by Kokoro TTS;
# fonts-dejavu-core = a font for the captions burned into the videos.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg espeak-ng fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies BEFORE copying the code, so a code change does not
# re-download torch (Docker re-uses this cached layer).
COPY requirements.txt requirements-ml.txt ./
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch==2.14.1 \
    && pip install --extra-index-url https://download.pytorch.org/whl/cpu -r requirements-ml.txt \
    && pip install -r requirements.txt

COPY . .
RUN mkdir -p /app/data /models

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
