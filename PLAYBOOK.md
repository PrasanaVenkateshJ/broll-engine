# PS3 Build Playbook — paste one phase at a time into Claude Code

How to use this file (Claude Code in VS Code)
- Unzip the starter kit to C:\dev\broll-engine and open that folder in VS Code.
- CLAUDE.md is read by Claude Code automatically in every conversation.
- Open Claude Code (Spark icon top-right of the editor, or Ctrl+Shift+P -> "Claude Code").
- Each phase = type `/phase 1`, `/phase 2` ... (custom command in .claude/commands).
  Claude first shows the plan; read it, then reply "go".
- `/explain <file>` = beginner explanation.  `/quiz` = interview practice.
  `/commit` = safe commit.  `/check` = full audit against the brief (run on Day 6).
- Start a NEW conversation for each phase (keeps Claude fast and focused).
- If Claude breaks something: hover a message -> Rewind (checkpoints) to undo its edits.
- .claude/settings.json pre-approves safe commands (docker, git, pytest, ffmpeg) so you
  are not asked every time, and blocks Claude from reading your .env keys.
- Do the phases IN ORDER. Do not start the next phase until "Done when" is all ticked.
- After each phase: run the check, read the explanation, answer the "Learn" questions
  out loud (this is your live-interview practice), then commit.
- The "Form rows" line tells you which Submission Form cells this phase gives evidence for.
  Write the evidence path into the form AS YOU GO, not on the last day.

Schedule: Day 1 = Phases 0-2 | Day 2 = 3-4 | Day 3 = 5-7 | Day 4 = 8-9 | Day 5 = 10-11 | Day 6 = 12

---------------------------------------------------------------------------------------
## Phase 0 — Machine setup (you do this by hand, about 45 min)

Open PowerShell **as Administrator** and run:

    wsl --install
    winget install -e --id Git.Git
    winget install -e --id Python.Python.3.11
    winget install -e --id Gyan.FFmpeg
    winget install -e --id Docker.DockerDesktop

Restart the PC. Open Docker Desktop once and accept (free for personal use).
Open a NEW PowerShell and check each one prints a version:

    git --version ; python --version ; ffmpeg -version ; docker --version ; docker compose version

Then:

    git config --global user.name "Your Name"
    git config --global user.email "the-email-you-use-on-github"
    cd C:\dev\broll-engine ; git init ; git add . ; git commit -m "chore: starter kit"
    code .

Unzip the starter kit into C:\dev\broll-engine (CLAUDE.md, PLAYBOOK.md, .claude\, AI_USE.md, .gitignore, .gitattributes).
Install the extension: VS Code -> Ctrl+Shift+X -> search "Claude Code" -> Install -> sign in.

Free accounts (email only, no card): GitHub (private repo `broll-engine`,
Settings -> Collaborators -> invite admin@incubrix.com), Pexels API key
(pexels.com/api), Pixabay API key (pixabay.com/api/docs), Groq API key (console.groq.com),
Hugging Face (huggingface.co) for deployment later.
Keep keys in a text file on your PC for now — never paste them into the repo.

Push to GitHub:

    git remote add origin https://github.com/<you>/broll-engine.git

---------------------------------------------------------------------------------------
## Phase 1 — Skeleton, config, logs, errors, health, Docker (Day 1)

PROMPT:
> Read CLAUDE.md. Create the project skeleton: .gitignore (include .env, data/, *.xlsx,
> testpack/outputs large files rule), .gitattributes, requirements.txt (pin versions),
> Dockerfile (python:3.11-slim + ffmpeg), docker-compose.yml with services api, worker,
> minio and minio-init (creates the bucket), .env.example listing every setting with a
> comment. Implement app/config.py, app/logging_setup.py (JSON logs with correlation id
> from X-Correlation-Id header, generated if missing, echoed in the response),
> app/errors.py (AppError + JSON handler for all errors incl. validation and 404/500),
> GET /health (process alive) and GET /ready (DB + MinIO reachable). Add 3 unit tests.
> Explain every file to me like a beginner. Give me the PowerShell commands to run it.

DONE WHEN:
- [ ] `docker compose up --build` starts all services
- [ ] http://localhost:8000/health returns {"status":"ok"}; /ready checks MinIO
- [ ] http://localhost:9001 shows the MinIO console with the bucket
- [ ] `docker compose run --rm api pytest` passes
- [ ] A bad URL returns the JSON error format

LEARN: What is a container? Why do api and worker run as separate services? What is an
env var and why are no settings hard-coded? What does the correlation id let you do?

COMMIT: "feat: project skeleton with config, JSON logs, errors, health"
FORM ROWS: Integration 1 (partly), 5, 6, 8

---------------------------------------------------------------------------------------
## Phase 2 — Jobs, idempotency, worker, progress, callback (Day 1)

PROMPT:
> Implement the job system from CLAUDE.md. POST /api/v1/broll/jobs accepts JSON
> (audio_url | script | scenes + voice, aspect_ratios, style, avoid, captions,
> callback_url) AND multipart upload (field `audio` + `options` JSON). Validate with
> Pydantic (exactly one input type; aspect ratios only 16:9, 9:16, 1:1). Return 202
> {job_id, status, links}. GET /api/v1/jobs/{job_id} returns status, progress 0-100,
> stage name, warnings, error, result links. Idempotency-Key on every POST (hash of the
> body stored with the key; same key+body -> same job; different body -> 409). Worker
> claims jobs atomically so two workers never take the same job; WORKER_CONCURRENCY
> setting. For now the pipeline is a fake step that sleeps and updates progress. When a
> job ends, POST to callback_url with X-IncuBrix-Signature (HMAC-SHA256 hex of the raw
> body using CALLBACK_SECRET), retry 3 times. Add API token auth (Authorization: Bearer,
> from API_TOKENS env; /health, /ready, /docs open) and a simple per-token rate limit.
> Write unit + integration tests. Explain it simply.

DONE WHEN:
- [ ] POST returns 202 and the job moves queued -> running -> succeeded with progress
- [ ] Same POST twice with the same Idempotency-Key returns the same job_id
- [ ] Two jobs at the same time both finish (two users case)
- [ ] Callback test verifies the HMAC signature
- [ ] Tests pass

LEARN: Why 202 and not 200? What would happen without idempotency if a phone retried?
What is HMAC and how does the receiver check it? How does the atomic claim work?

COMMIT: "feat: background jobs, idempotency, signed callbacks, auth"
FORM ROWS: Integration 3, 4; Must-have 7 (partly); Hard case 7 (two users)

---------------------------------------------------------------------------------------
## Phase 3 — Input intake + TTS (Day 2)

PROMPT:
> Implement pipeline/intake.py and pipeline/tts.py from CLAUDE.md. Intake: download URL
> with size/time limits (DOWNLOAD_FAILED, FILE_TOO_LARGE), check real file type with
> ffprobe not just the extension (UNSUPPORTED_FILE_TYPE, CORRUPTED_MEDIA), max 10 min
> (FILE_TOO_LONG), convert audio to 48 kHz mono WAV, empty/whitespace script ->
> EMPTY_SCRIPT. TTS adapter interface with `kokoro` (voice names mapped from e.g.
> en-IN-female via config) and `mock` (deterministic tone with correct-ish duration).
> Selected by TTS_PROVIDER env. Record model + version for the manifest. Tests use mock.
> Explain the adapter pattern to me.

DONE WHEN:
- [ ] mp3, wav and m4a uploads are accepted; a .txt renamed to .mp3 is rejected cleanly
- [ ] A script produces a narration WAV with Kokoro
- [ ] Tests pass with TTS_PROVIDER=mock

LEARN: What is an adapter and why does the brief demand one plus a mock? Why check the
file with ffprobe instead of trusting the extension?

COMMIT: "feat: input validation and TTS adapters"
FORM ROWS: Must-have 1 (partly); Hard case 6 (wrong file type, empty script)

---------------------------------------------------------------------------------------
## Phase 4 — Transcription, speech checks, scenes, captions (Day 2)

PROMPT:
> Implement pipeline/transcribe.py (faster-whisper with word timestamps and language
> auto-detect; model size from WHISPER_MODEL, default "small", int8 on CPU; mock
> adapter). Detect silent audio (ffmpeg silencedetect / volume) -> NO_SPEECH_DETECTED,
> and music-only audio (whisper finds almost no words or low speech probability over a
> loud signal) -> MUSIC_ONLY_AUDIO. Implement pipeline/scenes.py per the CLAUDE.md
> scene rules (contiguous, sentence then phrase boundaries, min/max scene length from
> config) and outputs.py SRT writer (max 2 lines, ~42 chars per line, timed to words).
> If input is a scene list, use one scene per item and time it from the TTS audio.
> Write unit tests for scenes.py with tricky cases (very long sentence, tiny sentence,
> no punctuation). Then make the worker produce a first test video with plain colour
> placeholder clips so I can see the whole flow end to end. Explain the algorithm.

DONE WHEN:
- [ ] A 60 s voiceover produces scenes with no gaps; check timeline in a JSON file
- [ ] captions.srt matches the voice when played in VLC
- [ ] Silent audio and music-only audio give clear errors
- [ ] FIRST FULL VIDEO (placeholder visuals + your voice + captions) appears in MinIO

LEARN: Explain the scene-splitting algorithm step by step. Why must the narration set the
timing? What happens to a 20-second sentence with no commas?

COMMIT: "feat: transcription, scene segmentation, SRT, first end-to-end video"
FORM ROWS: Must-have 2; Hard case 6 (silent, music-only)

---------------------------------------------------------------------------------------
## Phase 5 — Understanding + safety (Day 3)

PROMPT:
> Implement pipeline/understand.py with adapters `groq` (LLM_MODEL from env, JSON-mode
> output, timeout, 1 retry), `rules` (no network fallback: keyword-sense dictionary for
> value/bank/cloud/mouse/python etc., numbers -> statistic flag, simple capitalised-name
> detection) and `mock`. For each scene return: 3 visual search queries describing the
> intended meaning (use the whole script for context and the visual_hint if present),
> flags {abstract, statistic, named_person, brand, sensitive}, and a short reason.
> If groq fails, automatically use `rules` and add a warning. Implement
> pipeline/safety.py: named people/brands -> no faces, generic footage + warning;
> sensitive -> calm respectful query modifiers + exclude terms; apply `avoid` list as
> exclude terms. Tests for each double-meaning word and each hard case using `rules`.

DONE WHEN:
- [ ] "cloud computing" gives server/data-center queries, not sky
- [ ] "sales grew 40% in 2025" is flagged statistic
- [ ] A named person line produces a warning and no face query
- [ ] Turning the Groq key off still works (rules fallback + warning)

LEARN: Why an LLM AND a rule fallback? How do flags change what footage we search for?

COMMIT: "feat: meaning-based scene understanding and safety rules"
FORM ROWS: Must-have 5, 6; Hard cases 1, 2, 3, 4

---------------------------------------------------------------------------------------
## Phase 6 — Footage sources, ranking, no repeats (Day 3)

PROMPT:
> Implement providers/ with a common interface (search(query, orientation, min_duration)
> -> candidates with id, preview image, download url, duration, size, licence fields,
> attribution) for `pexels`, `pixabay`, `generated` (FFmpeg-made motion card: animated
> number/keyword text on a soft gradient, for statistics/abstract lines or as last
> fallback) and `mock`. Each provider has timeout + error handling; a failure is a
> warning, never a job failure; PROVIDER_ORDER and PROVIDERS_DISABLED from env (so a
> reviewer can switch one off). Implement rank.py: score each candidate's preview with
> open_clip against the scene meaning (mock scorer for tests), small bonus for duration
> fit and orientation fit, drop excluded terms, drop clips already used in this video,
> then apply the selection rule from CLAUDE.md and store the reason text and score.
> Download the chosen clip, verify with ffprobe, on failure try the next candidate.

DONE WHEN:
- [ ] Each scene gets a real, relevant clip and a reason in the timeline
- [ ] Setting PROVIDERS_DISABLED=pexels mid-job still finishes using pixabay/generated
- [ ] Repeat-clip test proves no clip id appears twice
- [ ] A fake corrupted download is skipped

LEARN: Explain the selection rule and why it is "measurable". What is CLIP doing?

COMMIT: "feat: multi-source footage with CLIP ranking and fallbacks"
FORM ROWS: Must-have 3, 5, 7; Advanced 1; Hard case 7

---------------------------------------------------------------------------------------
## Phase 7 — Rendering, QA, outputs (Day 3)

PROMPT:
> Implement render.py and qa.py per CLAUDE.md video rules. Per scene and per aspect
> ratio: trim/loop/slow the clip to the exact scene duration, scale + crop (salient-
> region crop for 9:16 using OpenCV saliency or edge energy on sample frames, blurred
> background pad if the subject cannot fit), 30 fps, yuv420p, brightness/colour
> normalisation, burn captions for that segment if requested. Concat segments with
> stream copy, mux the narration (AAC), and run QA (blackdetect, freezedetect,
> duration drift < 0.1 s); QA problems -> warning + retry with another clip. Write
> timeline.json and manifest.json (models, versions, settings, seed, stage timings,
> cost estimate, warnings, licences, ai_generated flags), upload everything to MinIO,
> return presigned links. Add an e2e test using all mock adapters.

DONE WHEN:
- [ ] One job gives video_16x9.mp4, video_9x16.mp4, video_1x1.mp4, captions.srt,
      timeline.json, manifest.json
- [ ] `ffprobe` shows 30 fps, h264, yuv420p, aac; durations match
- [ ] e2e test passes with no keys

LEARN: Why render each scene separately? What is yuv420p and why does it matter?

COMMIT: "feat: normalised multi-ratio rendering, QA checks, manifest"
FORM ROWS: Must-have 2, 3, 4; Advanced 3; Hard case 5; Integration 7, 9

---------------------------------------------------------------------------------------
## Phase 8 — Swap a clip + scene lists (Day 4)

PROMPT:
> Add POST /api/v1/broll/jobs/{job_id}/scenes/{scene_index}/swap (body: optional
> clip_url or provider+clip_id, or "next_best") as a background job with Idempotency-
> Key. Re-render only that scene's segments, re-concat, re-upload, update timeline and
> manifest (record timing of the swap vs the original full render). Confirm scene-list
> input with visual_hint fully works. Tests for both. Explain the caching.

DONE WHEN:
- [ ] Swap finishes in clearly less time than a full render (write both times down)
- [ ] Scene-list input uses the visual hints

COMMIT: "feat: partial re-render clip swap and scene-list input"
FORM ROWS: Advanced 2, 4

---------------------------------------------------------------------------------------
## Phase 9 — OpenAPI, tests, batch runner (Day 4)

PROMPT:
> Write openapi.yaml (OpenAPI 3.1) that exactly matches the running routes, schemas,
> headers (Idempotency-Key, X-Correlation-Id, Authorization) and the error schema. Add a
> test that compares the app's routes to openapi.yaml so they never drift. Create
> tools/batch_run.py: takes a folder or a JSONL file of inputs, submits each with an
> idempotency key, polls, downloads all outputs into one results folder with a
> summary.csv (status, duration, render time, warnings). Make one command run unit +
> integration + e2e tests. Update README sections for tests and batch runner.

DONE WHEN:
- [ ] `docker compose run --rm api pytest` runs everything green with no keys
- [ ] `python tools/batch_run.py testpack/inputs --out testpack/outputs/run1` works

COMMIT: "feat: openapi contract, contract test, batch runner"
FORM ROWS: Integration 1, 2, 10

---------------------------------------------------------------------------------------
## Phase 10 — Deploy free on Hugging Face Spaces (Day 5)

PROMPT:
> Prepare a free deployment on a Hugging Face Docker Space (CPU basic, port 7860) that
> keeps my GitHub repo private: the Space only contains a small Dockerfile + README
> that clones my private repo at build time using a GH_TOKEN build secret. In the
> container run api + worker + MinIO together with a small start script (one
> container). Secrets (API keys, API_TOKENS, CALLBACK_SECRET) as Space secrets.
> Enable rate limiting. Give me click-by-click steps and how to measure cold start.

DONE WHEN:
- [ ] Live URL /health works; a job from my laptop via the live URL succeeds
- [ ] Cold-start time measured and written down

FORM ROWS: Your Details (demo URL, token, cold start); Must-have 8

---------------------------------------------------------------------------------------
## Phase 11 — Evaluate honestly (Day 5)

1. Make 10 varied inputs in testpack/inputs: tutorial, news summary, product promo,
   story, 20 s to 5 min, your own voice in different accents + TTS voices, Hindi + Tamil
   if you can record them.
2. Make every hard-case input (abstract, stats, named person/brand, double meanings,
   sad news, edge subject 9:16, 5-min, silent, music-only, wrong type, empty script,
   provider down, corrupted download, two users at once).
3. Run the batch runner. Keep ALL outputs, including failures.
4. Watch every video, rate every scene 1-5 in testpack/ratings.csv.

PROMPT:
> Write tools/evaluate.py: reads ratings.csv and every timeline.json, computes average
> relevance, % of scenes rated 4-5, coverage (share of narration time with rating >= 3),
> repeat-clip rate, render success rate, Spearman correlation between CLIP score and my
> ratings, turnaround cold/warm for 60 s, cost per minute, peak memory, and how many
> concurrent jobs one server handled. Output a metrics.md table.

FORM ROWS: Core 2-3, Outstanding 1-4, Metrics tab, Hard cases tab

---------------------------------------------------------------------------------------
## Phase 12 — Hand-over (Day 6, finish by 6 PM)

PROMPT:
> Write README.md (what it does, mermaid architecture diagram, setup in under 10
> commands, tests, batch runner, every setting, all error codes), REPORT.md (2-4 pages:
> approaches considered, why this one, what works, what does not, measured numbers from
> metrics.md, cost per unit, next steps), SOURCES.md (every library, model, API, media
> source with licence), and testpack/INDEX.md linking every claim to a file. Do not
> invent any numbers — only use metrics.md.

Then by hand:
- Finish AI_USE.md honestly.
- Record ONE unedited screen video (max 8 min): one clean run, one failure handled,
  tests running, one design decision explained in code. (Use Windows key + Alt + R or
  OBS Studio, both free.)
- Fill the Excel form (Yes / Partial / No + evidence path for every row). Save as
  Assessment3_YourName.xlsx. Do NOT put it in the repo.
- Check the repo is accessible to admin@incubrix.com and the live demo works.
- Submit the Final Submission Form and reply to the email with the xlsx attached.
