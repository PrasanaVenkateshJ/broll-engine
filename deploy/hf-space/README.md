---
title: B-roll Studio
emoji: 🎬
colorFrom: indigo
colorTo: blue
sdk: docker
app_port: 7860
short_description: Voiceover or script in, matching B-roll videos out
pinned: false
---

# B-roll Studio

Turns a voiceover (or a plain script, voiced with Kokoro TTS) into B-roll videos that follow the
narration: 16:9, 9:16 and 1:1 MP4s with burned-in captions, plus `captions.srt`, `timeline.json`
and `manifest.json` (licences, models, timings).

- **Web UI:** open this Space and paste the API token you were given into the top bar.
- **API:** `POST /api/v1/broll/jobs` with `Authorization: Bearer <token>`; see `openapi.yaml` in the
  project repository. `GET /health` and `GET /ready` need no token.

Notes for reviewers:

- **Cold start:** after a restart or a sleep the Space needs a few minutes before `/ready` answers.
  The models are already inside the image, so nothing is downloaded at start.
- **Storage is temporary.** Jobs and output files live inside the running container and are lost
  when the Space restarts. Download what you want to keep.
- **Rate limits are on** (per token): a small number of new jobs per minute; status checks and
  downloads have a higher limit. Over the limit the API answers `429 RATE_LIMITED` with `Retry-After`.
- One job runs at a time (free CPU hardware, 2 vCPU). A short script takes a few minutes.

This folder is the only thing pushed to the Space. The Dockerfile fetches the application code
from the (private) source repository at build time, using a build secret.
