# AI_USE.md — AI tools used in this project

| Tool | Used for |
|---|---|
| Claude Code (VS Code extension) | Pair-programming: scaffolding, writing code and tests from my design rules in CLAUDE.md, explaining code, debugging. I reviewed, ran and can explain every change. |
| Claude (claude.ai) | Analysing the brief and planning the architecture and build phases. |

## Log (one line per step)
- Phases 1-2: Claude Code built the skeleton (config, JSON logs, errors, health/ready, Dockerfile, compose, CI), the job API (JSON + upload), idempotency, signed download links, auth + rate limit, worker with atomic claim, signed callbacks and 48 tests (47 pass locally, 1 MinIO test runs only in Docker/CI).
- Phases 3-4: Claude Code wrote intake.py (download limits, ffprobe real-type checks, 48 kHz mono WAV), tts.py (kokoro + mock adapters), transcribe.py (faster-whisper + mock, silent / music-only checks), scenes.py (scene-splitting algorithm), outputs.py (SRT), a first render.py (colour placeholders + burned captions), runner.py (real worker stages) and the tests; installed the ML packages in my .venv and ran one real Kokoro + Whisper job locally. I reviewed and ran it.
- Phases 5-7: Claude Code wrote understand.py (groq / rules / mock, sense dictionary, statistic / name / brand / sensitive detectors, Groq-to-rules fallback), safety.py, providers/ (pixabay_video, pixabay_image_motion, wikimedia with a CC0 / PD / CC BY-only licence filter, generated motion cards, mock, disabled pexels, mid-job override file), rank.py (open_clip scoring + measurable selection rule, ffprobe-verified downloads), the new render.py (time fitting, salient 9:16 crop / blurred fit, Ken Burns, brightness/colour normalisation, segment cache), qa.py, timeline/manifest builders, an SSRF guard, and 106 new mock tests (213 total: 212 pass, 1 MinIO test runs only in Docker/CI). It ran one real local job (Kokoro, Whisper, Groq, Pixabay, CLIP); that run exposed two bugs (full-range yuvj420p output from JPEG photos; Wikimedia returning 403 without a contact in the User-Agent), which it fixed and covered with tests. I reviewed and ran it.
