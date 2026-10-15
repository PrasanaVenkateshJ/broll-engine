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
| PyTorch (CPU) | 2.14.1 | model runtime (from Phase 3) | BSD-3-Clause |
| faster-whisper | 1.2.1 | transcription (from Phase 4) | MIT |
| Kokoro | 0.9.4 | text-to-speech (from Phase 3) | Apache-2.0 |
| Kokoro-82M model weights (`hexgrad/Kokoro-82M`) | downloaded at first use | TTS voices | Apache-2.0 |
| misaki (installed by Kokoro) | 0.9.4 | text -> phonemes for Kokoro | Apache-2.0 |
| spaCy + en_core_web_sm | 3.8.16 / 3.8.0 | English word analysis inside misaki | MIT |
| faster-whisper `small` model (`Systran/faster-whisper-small`) | downloaded at first use | speech -> words | MIT |
| CTranslate2 (installed by faster-whisper) | 4.8.3 | runs the Whisper model on CPU | MIT |
| Silero VAD (bundled in faster-whisper) | bundled | skips non-voice parts | MIT |
| DejaVu fonts (`fonts-dejavu-core`, Docker) | distro | font for burned-in captions | Bitstream Vera / public domain |
| open_clip_torch | 3.3.0 | clip scoring (from Phase 6) | MIT |
| FFmpeg (Debian package) | distro | audio/video processing, called as a separate program | LGPL/GPL (separate binary) |
| espeak-ng (Debian package) | distro | phonemes for Kokoro, separate program | GPL-3.0 (separate binary) — review |
| MinIO server (`pgsty/minio` image) | RELEASE.2026-08-04 | S3 storage service in docker compose only | AGPL-3.0 (allowed only as the storage service) |
| python:3.11-slim image | 3.11 | container base | PSF + Debian licences |
