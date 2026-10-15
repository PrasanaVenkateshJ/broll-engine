# CLAUDE.md — Project rules for the B-roll Video Engine (IncuBrix Assessment 3)

You are helping a BEGINNER build this project alone. They must be able to explain and
change every line in a live interview. So, ALWAYS:
1. Work in small steps. Do only the task asked, then stop.
2. After writing code, explain it simply: what each file does, why it exists, and what
   would break if it changed. Use plain English, no jargon without a one-line meaning.
3. Tell the user exactly which command to run to check it works (Windows PowerShell).
4. Suggest a short git commit message at the end of every working step.
5. Add one line to AI_USE.md describing what you (Claude Code) did in this step.
6. Never invent results, numbers or evidence. If something does not work, say so.

## What the product does
Input: a voiceover (mp3/wav/m4a, up to 10 min, by upload or URL), OR a plain script
(we generate the voice with TTS), OR an IncuBrix scene list [{text, visual_hint}].
Output per job: video_16x9.mp4 / video_9x16.mp4 / video_1x1.mp4 (H.264 + AAC, 30 fps,
1080p-class, yuv420p), captions.srt, timeline.json, manifest.json — stored in S3
(MinIO) and returned as download links.

## Fixed tech stack (do not change without asking)
- Python 3.11, FastAPI + Uvicorn, Pydantic v2 settings
- SQLAlchemy + SQLite (file in a Docker volume) for jobs and idempotency keys
- Separate `worker` process that claims queued jobs from the DB (atomic UPDATE)
- FFmpeg / ffprobe for all audio + video work (called via subprocess)
- faster-whisper (MIT) for transcription with word timestamps
- Kokoro TTS (Apache-2.0) for text -> voice
- open_clip (MIT) for scoring clip thumbnails against scene text
- boto3 -> MinIO (S3-compatible) for storage
- pytest for tests; httpx for HTTP calls
- Docker + docker compose (services: api, worker, minio, minio-init)

## Folder layout
app/
  main.py            FastAPI app, routes, middleware (correlation id, auth, rate limit)
  config.py          ALL settings from env vars (nothing hard-coded)
  errors.py          AppError + error codes + JSON error handler
  logging_setup.py   JSON logs: timestamp, level, message, job_id, correlation_id
  db.py, models.py   SQLAlchemy engine + Job / IdempotencyKey tables
  jobs.py            create job, idempotency, progress updates, HMAC callback
  storage.py         S3 upload + presigned download links
  worker.py          loop: claim job -> run pipeline -> save result
  pipeline/
    intake.py        validate + fetch input, ffprobe checks, limits
    tts.py           adapters: kokoro | mock
    transcribe.py    adapters: whisper | mock ; speech / music / silence detection
    scenes.py        split words into scenes on sentence/phrase boundaries
    understand.py    adapters: groq | rules | mock -> visual queries + flags
    safety.py        named people, brands, sensitive topics, avoid-list rules
    providers/       footage adapters: pexels | pixabay | generated | mock
    rank.py          scoring + selection rule + no-repeat
    render.py        normalise, crop per ratio, colour, captions, concat, mux
    qa.py            black / frozen frames, duration drift checks
    outputs.py       timeline.json, manifest.json, captions.srt
tools/batch_run.py   CLI batch runner
tests/               unit/, integration/, e2e/ (all run with mocks, no keys)
testpack/            inputs/, outputs/, logs/, ratings.csv, INDEX.md

## Company rules (IncuBrix Integration Standard — must follow)
1. `docker compose up` starts everything; `docker compose run --rm api pytest` runs all
   tests with NO API keys (mock adapters). `.env.example` lists every setting.
2. `openapi.yaml` (OpenAPI 3.1) must match the running API exactly. All routes under /api/v1/
   (except /health and /ready).
3. Slow work is a job: POST returns 202 + job_id. GET /api/v1/jobs/{job_id} returns
   status (queued|running|succeeded|failed), progress 0-100, result links.
   Optional callback_url, signed: X-IncuBrix-Signature = hex HMAC-SHA256(body, secret).
4. Every POST accepts Idempotency-Key: same key + same body -> same job, no duplicate work.
   Same key + different body -> 409 IDEMPOTENCY_KEY_REUSED.
5. Errors always: {"error": {"code", "message", "retryable", "details"}}, codes UPPER_SNAKE_CASE,
   all listed in README.
6. Every setting, path, model name and limit comes from env vars (config.py).
7. Inputs by upload OR URL. Outputs to S3/MinIO, returned as presigned links.
8. Structured JSON logs incl. job_id and X-Correlation-Id. GET /health, GET /ready.
9. manifest.json: models/providers + versions, settings, seed, timings, cost estimate,
   warnings, licence/rights info, ai_generated flags.
10. tools/batch_run.py runs a folder/file of inputs against the API into one output folder.

## Error codes (keep this list in sync with README)
INVALID_INPUT, EMPTY_SCRIPT, UNSUPPORTED_FILE_TYPE, FILE_TOO_LONG, FILE_TOO_LARGE,
CORRUPTED_MEDIA, DOWNLOAD_FAILED, NO_SPEECH_DETECTED, MUSIC_ONLY_AUDIO,
UNSUPPORTED_ASPECT_RATIO, JOB_NOT_FOUND, SCENE_NOT_FOUND, IDEMPOTENCY_KEY_REUSED,
UNAUTHORIZED, RATE_LIMITED, PROVIDER_UNAVAILABLE, RENDER_FAILED, INTERNAL_ERROR

## Pipeline rules (the "brain" of the product)
- Narration sets the timing. Scenes are contiguous: scene[0].start = 0,
  scene[i+1].start = scene[i].end, last scene ends at audio end. No gaps.
- Cut only at sentence ends, then commas/phrase pauses. Target 2.5-7 s per scene;
  merge anything < MIN_SCENE_SECONDS (default 2.0), split > MAX_SCENE_SECONDS (default 8.0).
- Each scene gets: a visual query that means the SENSE of the line (e.g. "cloud computing"
  -> "data center server racks"), flags: abstract, number, named_person, brand, sensitive.
- Safety: never generate faces; for named people/brands use generic related footage and
  add a warning; sensitive topics -> calm, respectful footage, no graphic content;
  honour the `avoid` list as negative filters.
- Selection rule (measurable, store the reason in timeline.json):
  use the best stock clip if its CLIP score >= STOCK_MIN_SCORE (default 0.22) and it is
  not already used; otherwise use the next provider; if nothing passes, use the
  `generated` source (FFmpeg motion card, e.g. animated "40%" for statistics), flagged
  generated=true, ai_generated=false.
- No clip id is used twice in one video.
- A provider error or timeout never fails the job: log a warning, try the next provider.
- Corrupted downloads (ffprobe fails) -> try the next candidate.
- Every clip records: provider, provider_clip_id, url, licence name + URL, attribution,
  commercial_use=true, watermark=false, ai_generated, reason, relevance score.

## Video rules
- 30 fps, yuv420p, H.264 (libx264), AAC 48 kHz. 16:9=1920x1080, 9:16=1080x1920, 1:1=1080x1080.
- Clips shorter than their scene: slow slightly or loop smoothly, never freeze.
- 9:16 from a 16:9 clip: crop around the most salient region (not blindly the center).
- Normalise brightness/colour across clips.
- Render each scene as its own segment (captions burned per segment) so a clip swap
  re-renders only one segment, then concat with stream copy.
- QA after render: ffmpeg blackdetect + freezedetect, |video - audio duration| < 0.1 s.

## Licence + secret rules
- Only MIT / Apache / BSD / similar permissive code and models in the default path.
  No CC BY-NC or research-only models/media. Avoid AGPL (MinIO is allowed only as the
  compose storage service). Record every dependency in SOURCES.md.
- Footage only from Pexels / Pixabay (commercial use allowed, no watermark) or generated.
- API keys only in `.env` (git-ignored). Never print keys in logs. Never commit `.env`,
  the filled Submission Form, or personal details.

## Windows notes
- The user runs Windows + Docker Desktop (WSL2). Give PowerShell commands.
- Keep `.gitattributes` with `* text=auto eol=lf` so shell scripts work in Linux containers.
