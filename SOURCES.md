# SOURCES.md — every dependency and its licence

| Name | Version | Used for | Licence |
|---|---|---|---|
| FastAPI | 0.143.2 | HTTP API | MIT |
| Uvicorn | 0.54.0 | ASGI web server | BSD-3-Clause |
| Pydantic / pydantic-settings | 2.14.1 / 2.16.0 | validation, settings from env | MIT |
| python-multipart | 0.0.32 | multipart file uploads | Apache-2.0 |
| SQLAlchemy | 2.1.4 | database access (SQLite) | MIT |
| httpx | 0.28.1 | HTTP client (callbacks, tests) | BSD-3-Clause |
| boto3 | 1.43.114 | S3 / MinIO client | Apache-2.0 |
| pytest | 9.1.1 | tests | MIT |
| PyYAML | 6.0.3 | reads openapi.yaml in the contract test | MIT |
| PyTorch (CPU) | 2.14.1 | model runtime (from Phase 3) | BSD-3-Clause |
| torchvision (CPU) | 0.29.1 | image transforms used by open_clip (must match torch 2.14.1) | BSD-3-Clause |
| faster-whisper | 1.2.1 | transcription (from Phase 4) | MIT |
| Kokoro | 0.9.4 | text-to-speech (from Phase 3) | Apache-2.0 |
| Kokoro-82M model weights (`hexgrad/Kokoro-82M`) | downloaded at first use | TTS voices | Apache-2.0 |
| misaki (installed by Kokoro) | 0.9.4 | text -> phonemes for Kokoro | Apache-2.0 |
| spaCy + en_core_web_sm | 3.8.16 / 3.8.0 | English word analysis inside misaki | MIT |
| faster-whisper `small` model (`Systran/faster-whisper-small`) | downloaded at first use | speech -> words | MIT |
| CTranslate2 (installed by faster-whisper) | 4.8.3 | runs the Whisper model on CPU | MIT |
| Silero VAD (bundled in faster-whisper) | bundled | skips non-voice parts | MIT |
| DejaVu fonts (`fonts-dejavu-core`, Docker) | distro | font for burned-in captions | Bitstream Vera / public domain |
| open_clip_torch | 3.3.0 | scores footage previews against scene meaning | MIT |
| open_clip `ViT-B-32` weights `laion2b_s34b_b79k` (`laion/CLIP-ViT-B-32-laion2B-s34B-b79K`) | downloaded at first use | CLIP model for ranking | MIT |
| NumPy | 2.4.6 | edge/motion energy for the salient 9:16 crop | BSD-3-Clause |
| Pillow | 12.3.0 | opens preview images for CLIP | MIT-CMU (HPND) |
| FFmpeg (Debian package) | distro | audio/video processing, called as a separate program | LGPL/GPL (separate binary) |
| espeak-ng (Debian package) | distro | phonemes for Kokoro, separate program | GPL-3.0 (separate binary) — review |
| MinIO server (`pgsty/minio` image) | RELEASE.2026-08-04 | S3 storage service in docker compose, and the same unmodified binary copied into the Hugging Face Space image (internal storage only) | AGPL-3.0 (allowed only as the storage service) |
| python:3.11-slim image | 3.11 | container base | PSF + Debian licences |
| git, curl (Debian packages, Space image only) | distro | clone the code at build time; health checks in start.sh | GPL-2.0 / curl licence (separate programs) |
| huggingface_hub | 2.2.0 (installed with faster-whisper / Kokoro) | model downloads; `tools/deploy_hf.py` uploads the Space folder | Apache-2.0 |
| modal (Python client, NOT in requirements.txt; only on the deploying PC) | 1.6.2 | deploys the live demo (`deploy/modal/modal_app.py`) | Apache-2.0 |
| debian_slim image (Modal) | Python 3.11 | base of the Modal demo image | PSF + Debian licences |
| cloudflared (downloaded on demand by `scripts/tunnel.ps1`, not committed) | latest release | fallback demo: Cloudflare quick tunnel | Apache-2.0 |

## External APIs and media (footage + understanding)

| Name | Used for | Terms / licence of what we use |
|---|---|---|
| Pixabay Videos API (`pixabay_video`) | stock video clips | Pixabay Content License: commercial use allowed, no attribution required, no watermark on API downloads (https://pixabay.com/service/license-summary/). Key in `.env` only. |
| Pixabay Images API (`pixabay_image_motion`) | stock photos, animated with a Ken Burns zoom in FFmpeg | Pixabay Content License (same as above) |
| Wikimedia Commons API (`wikimedia`) | photos, animated with a Ken Burns zoom | No key. ONLY CC0, Public Domain and CC BY (any version) accepted; CC BY-SA, NC, ND, GFDL-only and unknown licences are rejected in code. Author + licence URL recorded as attribution in timeline.json / manifest.json. User-Agent must contain a contact. |
| `generated` motion cards | statistics / abstract lines / last fallback | Made locally by FFmpeg (gradient + text). No third-party media, no AI model: generated=true, ai_generated=false. |
| Pexels Videos API (`pexels`) | optional only | Pexels License. Pexels paused new API keys, so the project does not depend on it; skipped while `PEXELS_API_KEY` is empty. |
| Groq API (`UNDERSTAND_PROVIDER=groq`) | scene meaning -> search queries + flags | Groq free tier. Default model `openai/gpt-oss-120b` (Apache-2.0 weights). Falls back to offline rules if it fails. Key in `.env` only. |
| Modal (modal.com) | hosting of the live demo | Paid-per-second cloud service (free monthly credit); our code stays under our control. Secrets only in the Modal secret `broll-secrets`. |
| Cloudflare quick tunnels (trycloudflare.com) | fallback demo URL, no account | Cloudflare's free quick-tunnel service (for testing/demos, no uptime guarantee). |

## Hosting (live demo)

| Name | Used for | Terms |
|---|---|---|
| Hugging Face Spaces (Docker, CPU basic, free) | public live demo of the API + Studio | Hugging Face Terms of Service. Secrets are set in the Space settings, never in the repository. |
| GitHub Actions (`keepalive.yml`) | GET /health every 6 hours so the free Space does not sleep | GitHub Terms; free minutes of the account |
