# Resilience tests (local, real adapters)

Each test has its own output folder under `testpack/outputs/resilience/` (job files in the batch_run layout +
`events.json` = what the test did, when) and its own worker log under `testpack/logs/`.
All four ran on 2026-10-17 against the local API (`uvicorn app.main:app`) and worker (`python -m app.worker`)
with my real `.env` (Kokoro, Whisper, Groq, Pixabay, Wikimedia, CLIP, `STORAGE_BACKEND=local`).
The inputs are in `testpack/resilience/inputs/`. Results: see "Results" at the end (written after the runs).

| Test | What is broken | How | Pass when |
|---|---|---|---|
| (a) provider switched off mid-job | `pixabay_video` disappears while the job is running | `tools/resilience.py switch-off`: waits until the job is in the footage stage and has picked its first scene, then writes `{"disabled": ["pixabay_video"]}` to `PROVIDERS_OVERRIDE_FILE` (`./data/providers_override.json`); removes it at the end | job `succeeded`; scenes chosen after the switch use other sources and their `reason` says `pixabay_video skipped (switched off by ...)` |
| (b) corrupted download | the first real `pixabay_video` download arrives cut to 4 KB (ffprobe cannot read it) | the normal worker is stopped and `tools/fault_worker.py --provider pixabay_video --count 1` runs instead (fault injection only in this test tool); the job is sent with `tools/batch_run.py` | job `succeeded`; the worker log shows `FAULT INJECTION`; the scene's `reason` says the clip was corrupted and the next candidate was used; a warning in the job |
| (c) two users at once | two different bearer tokens post at the same moment | the API ran with `API_TOKENS=dev-token,eval-user-b`, the worker with `WORKER_CONCURRENCY=3`; `tools/resilience.py two-users` posts both jobs behind a thread barrier, then each user asks for the other user's job | both jobs `succeeded`, their run times overlap, and each user gets 404 `JOB_NOT_FOUND` for the other's job |
| (d) Groq outage | the LLM answers 401 (bad key) | a worker started with `GROQ_API_KEY=invalid-key-for-resilience-test` (environment beats `.env`) for one job | job `succeeded`; manifest `models.understand.fallback = "rules"`; warning `Scene understanding with Groq failed (HTTP 401) ... used the offline rules instead` |

## Results (2026-10-17, all four passed)

| Test | Output folder | Log | Result |
|---|---|---|---|
| (a) switch-off | `testpack/outputs/resilience/a_switch_off/` (`events.json`) | `testpack/logs/resilience_a_switch_off.log`, worker log `testpack/logs/worker_concurrency3.log` | **Pass.** `pixabay_video` switched off at 13:41:55 UTC while the job was in the footage stage (progress 61, first scene picked). Scenes 0-1 (chosen before) use `pixabay_video`; scenes 2-6 use `pixabay_image_motion` and their reason says `pixabay_video skipped (switched off by ./data/providers_override.json)`. Job `succeeded` in 182.4 s; override file removed afterwards. |
| (a) first attempt | `testpack/outputs/resilience/a_switch_off_attempt1/` | `testpack/logs/resilience_a_switch_off_attempt1.log` | Same result (job succeeded, scenes 2-6 skipped pixabay_video), but my test tool's `skipped_note` check was wrong (it also matched scenes that USED pixabay_video). Tool fixed, test re-run as above; kept for honesty. |
| (b) corrupted download | `testpack/outputs/resilience/b_corrupt_download/` | `testpack/logs/worker_resilience_b_fault.log`, `testpack/logs/resilience_b_batch.log` | **Pass.** Log: `FAULT INJECTION: download of pixabay_video:286275 cut from 8478184 to 4096 bytes`. Warning: `Scene 0: downloaded clip pixabay_video:286275 is corrupted (the video data cannot be decoded); tried the next candidate`; scene 0 uses `pixabay_video:286278`. A REAL fault happened in the same scene too: an earlier candidate (`pixabay_video:160867`) took longer than 60 s to download and was skipped. Job `succeeded` in 388.9 s (the 60 s download timeout made it slow). |
| (c) two users | `testpack/outputs/resilience/c_two_users/` (`events.json`) | `testpack/logs/resilience_c_two_users.log`, `testpack/logs/worker_concurrency3.log` | **Pass.** Both posted at 13:32:35 UTC; both ran at the same time (started 13:32:37, finished 13:36:41 / 13:36:34) and `succeeded` (246.9 s / 240.9 s). Each user reading the other's job got HTTP 404 `JOB_NOT_FOUND`. |
| (d) Groq outage | `testpack/outputs/resilience/d_groq_outage/` | `testpack/logs/worker_resilience_d_groq_outage.log`, `testpack/logs/resilience_d_batch.log` | **Pass.** `groq attempt 1 failed: HTTP 401`, `groq attempt 2 failed: HTTP 401`; warning `Scene understanding with Groq failed (HTTP 401); used the offline rules instead`; manifest `models.understand.fallback = "rules"`. Job `succeeded` in 133.2 s. The bad key does not appear in the log. The rules' queries are weaker (scene 0 "data center server racks" is right; scenes 1-2 are bare keywords such as "files stored safely"). |

Not part of these four, but seen in run1 (real, not injected): Groq answered HTTP 429 for the 61-scene 5-minute
voiceover and left out one scene for core01; both jobs fell back to the rules with a warning and succeeded.
