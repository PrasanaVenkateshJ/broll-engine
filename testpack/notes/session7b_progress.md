# Session 7B: progress notes (paused 2026-10-17)

Paused safely: no job, batch, server, worker or sampler is running. Nothing is committed.

## DONE

### Code changes (all uncommitted, see `git status`)
- **A1 Statistic overlay**: a statistic line gets real topic footage with the number as a badge
  (`overlay: true` plus a reason in timeline.json). A plain number card is used only if no clip passes.
  Files: `cards.py`, `rank.py`, `render.py` (`write_badge_ass`), `outputs.py`.
- **A2 Metaphors**: abstract lines get 3-5 concrete metaphors (`METAPHORS` in `understand.py`).
- **A3 Searching**:
  - 5 queries per scene (meaning, metaphor, setting, action, mood), 2 result pages, de-duplicated,
    merged round-robin, at most 48 candidates per source.
  - New Openverse provider (`providers/openverse.py`): CC0, PDM and CC BY with a named creator only,
    with strict licence tests.
- **A4 Scoring**:
  - CLIP prompt ensembling and the mean of the best 3 queries.
  - Up to 3 frames for the best videos.
  - Penalty for text-heavy frames.
  - Video preferred unless the photo is more than 0.03 better.
- **A5 Adaptive threshold**: a clip scoring at least 0.1936 is accepted only if the judge rates it 4 or higher.
- **A6 VLM judge** (`vlm.py`): Groq `qwen/qwen3.8-27b`, which is Apache-2.0 but a Groq "Preview" model.
  It gets one contact sheet per scene and 4 scenes per job at most. There is a mock adapter, and
  `vlm_score` is recorded in timeline.json. It is OFF by default (`VLM_PROVIDER=none`) and turned on
  for run2.
- **B7 Groq understanding**:
  - Batches of 12 scenes.
  - On a 429 it waits for the `retry-after` time.
  - It asks again only for the missing scenes.
  - Any scene still missing falls back to the offline rules, for that scene only.
- **B8 Number words**: English, Hindi and Tamil number words (`cards.py`, `CARD_RULES_VERSION=3`).
- **B9 Sensitive stories**: if the whole story is sad, every scene gets the calm rules
  (`story_is_sensitive`, `safety.py`).
- **B10 Crop**: a near-tie crop now keeps the subject whole (localised-detail profile in `render.py`).
  The sailboat test fixture is in `tests/fixtures/`.
- **B11 Speed-ups**:
  - TTS cache.
  - Parallel segment rendering (`RENDER_WORKERS`).
  - The likely winning clip is prefetched in pass 1.
  - Logo checks run in smaller batches.
  - A per-minute rate limiter and a cooldown after 429s.
- **B12 Modal memory**: `deploy/modal/modal_app.py` now requests MEMORY_MB 4608 with LIMIT 7168.
  **`testpack/notes/cost_modal.md` is NOT updated yet.**
- **Bug fixes**:
  - A torch import race in `worker.py` (Kokoro ImportError).
  - Pixabay `isAiGenerated` is now read.
  - The footage cache now lasts 24 h, as Pixabay's terms require.
  - A misleading "best score < 0.22" note that appeared when the judge dropped every passing clip.
- **Docs updated**: `.env.example`, `README.md` (settings table), `CLAUDE.md`, `SOURCES.md` and `openapi.yaml`.
- **New tool**: `tools/compare_runs.py` writes `testpack/compare_run1_run2.md`. It is not run yet.

### Tests
- Last full run: 454 collected, 452 passed, 2 skipped (the Playwright browser test and the MinIO-only test).
- `tests/unit/test_compare_runs.py` (added afterwards) passes.
- New test files:
  - `test_footage_quality.py` (36 tests)
  - `test_understand_robustness.py` (9 tests)
  - `test_compare_runs.py`
  - New parametrized number-word tests in `test_cards.py`

### Measurements so far (real adapters, local, VLM_PROVIDER=groq)
- **Smoke job hard01** (before pausing):
  - Clips: 5 videos and 1 photo, 0 cards (run1 had 3 cards). There were 2 number badges.
  - Judge ratings 4-5 (mean 4.33). 6 judge calls, 2 hit a 429, 38 s spent waiting.
- **60 s perf jobs**, in `testpack/outputs/perf_run2/`; logs in `testpack/logs/perf_run2_*.log`:

| Case | run1 turnaround | run2 turnaround | run2 stages (tts / transcribe / understand / footage / render, s) |
|---|---|---|---|
| Cold 60 s (`cold_60s`) | 328.8 s | **408.0 s** (done) | 43.3 / 33.9 / 12.1 / 216.1 / 95.6 |
| Warm, same script (`warm_same_script`) | 280.6 s | **476.1 s** (done) | 0.4 / 54.2 / 7.2 / 284.9 / 117.2 |
| Warm, new script (`warm_new_script`) | 256.6 s | **not finished**: stopped at 52% | - |

  Run1 cold stages were tts 40, transcribe 21, footage 132, render 127.

  Honest reading:
  - Render got faster (127 → 96 s).
  - The TTS cache works (43 → 0.4 s on the same script).
  - The footage stage is much slower (132 → 216-285 s) because many more candidates are scored and
    judged, so whole jobs are SLOWER than in run1.
  - Warm-same being slower than cold, and transcribe at 54 s, are not explained yet. They could be
    judge 429 waits or laptop load. Check `worker_run2.log` before reporting.
- **Memory** (`testpack/logs/memory_run2.csv`, covers the 2 finished perf jobs and part of the third):
  - Peak total 4,334 MB at 15:50:17Z (worker 3,294 MB peak, ffmpeg 1,909 MB peak).
  - Run1 peak was 4,264 MB.
  - Still under the new Modal request of 4,608 MB.
  - `peaks_after_run2.txt` is not made yet.

### run2 test pack (23 inputs)
- **Not started.** `testpack/outputs/run2/` does not exist yet, so no input has finished.

## HALF-FINISHED: must be re-run
- `testpack/outputs/perf_run2/warm_new_script/perf_b_sleep/`: the job was stopped at 52%. It has no
  summary.csv, and `testpack/logs/perf_run2_warm_new.log` is incomplete. In `data/run2.db`, job
  `893e39e68a5e4aeea87aa8b5e9fce7a9` is left "running". The worker's restart recovery should mark it
  failed (JOB_INTERRUPTED).

  Before re-running, move the half folder aside:
  `Move-Item testpack\outputs\perf_run2\warm_new_script testpack\outputs\perf_run2\_interrupted_warm_new`

## LEFT (in this order)
1. Restart the servers (commands below). Wait until `/ready` answers and the worker log shows "CLIP model loaded".
2. Warm-new perf case. It needs a worker that has already done one job, so first run a throw-away
   warm-up job (do not keep it), then:
   ```powershell
   New-Item -ItemType Directory -Force $env:TEMP\pb | Out-Null
   Copy-Item testpack\perf\inputs\perf_b_sleep.json $env:TEMP\pb\
   .venv\Scripts\python tools\batch_run.py $env:TEMP\pb --out testpack\outputs\perf_run2\warm_new_script --ratios 16:9,9:16 --concurrency 1 *> testpack\logs\perf_run2_warm_new.log
   ```
3. Full run2 test pack (same inputs and options as run1). It takes a few hours. Write down the start and finish times:
   ```powershell
   (Get-Date).ToUniversalTime().ToString("o") | Out-File testpack\logs\run2_started_at.txt
   .venv\Scripts\python tools\batch_run.py testpack\inputs --out testpack\outputs\run2 --ratios 16:9,9:16 --keep-input-ratios --concurrency 1 *> testpack\logs\run2_batch.log
   (Get-Date).ToUniversalTime().ToString("o") | Out-File testpack\logs\run2_finished_at.txt
   ```
4. Stop the sampler. Write the peaks to `testpack/logs/peaks_after_run2.txt` with `tools/process_peaks.ps1`.
   If the peak goes above 4,608 MB, raise `MEMORY_MB` in `deploy/modal/modal_app.py`.
5. Update `testpack/notes/cost_modal.md` honestly. The new request is 2 CPU and 4.5 GiB:
   2×0.0000131 + 4.5×0.00000222 = $0.00003619 per second. Recompute the tables with the run2
   turnarounds and keep them marked as an estimate.
6. Check the run2 results for these inputs:
   - hard04: all scenes have `story_sensitive`.
   - hard05: sailboat crop in scene 2 at 9:16.
   - hard06: Groq batches, with no whole-job rules fallback.
   - core11-13: number words shown as badges.
   - Is Openverse used anywhere?
7. Rating page: `.venv\Scripts\python tools\make_rating_page.py --runs testpack/outputs/run2`
8. Video pack: `.venv\Scripts\python tools\make_video_pack.py`
9. Comparison: `.venv\Scripts\python tools\compare_runs.py`, which writes `testpack\compare_run1_run2.md`.
10. `testpack/INDEX.md`: use the PLAYBOOK.md "FORM ROWS" numbering (Must-have 1-8, Advanced 1-4,
    Integration 1-10, Hard cases 1-7) with Yes/Partial/No and evidence paths.
    Note: `testpack/FORM_ROWS.md` does not exist; the rows are in PLAYBOOK.md.
11. Run all tests again: `.venv\Scripts\python -m pytest`
12. Add one line to `AI_USE.md`, then write the final report:
    - run1 vs run2 numbers, taken from the files only
    - the speed before and after
    - the VLM preview caveat and its 8K tokens/min limit
    - a suggested commit message (do not commit)

## Restart the servers (PowerShell, from C:\dev\broll-engine)
This uses the same separate run2 database, storage and caches as before. It uses NEW log file names,
so the logs from today are kept (with `>` the old files would be overwritten).

Window 1, API:
```powershell
cd C:\dev\broll-engine
$env:DATABASE_URL="sqlite:///./data/run2.db"; $env:LOCAL_STORAGE_DIR="./data/storage_run2"
$env:FOOTAGE_CACHE_DIR="./data/footage_cache_run2"; $env:TTS_CACHE_DIR="./data/tts_cache_run2"; $env:VLM_PROVIDER="groq"
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 *> testpack\logs\api_run2b.log
```
Window 2, worker (same 5 `$env:` lines first):
```powershell
cd C:\dev\broll-engine
$env:DATABASE_URL="sqlite:///./data/run2.db"; $env:LOCAL_STORAGE_DIR="./data/storage_run2"
$env:FOOTAGE_CACHE_DIR="./data/footage_cache_run2"; $env:TTS_CACHE_DIR="./data/tts_cache_run2"; $env:VLM_PROVIDER="groq"
.venv\Scripts\python -m app.worker *> testpack\logs\worker_run2b.log
```
Window 3, memory sampler:
```powershell
cd C:\dev\broll-engine
powershell -NoProfile -File tools\sample_memory.ps1 -Out testpack\logs\memory_run2b.csv
```
Check that it is up: `Invoke-RestMethod http://localhost:8000/ready`

The API must stay on port 8000, because the download links use PUBLIC_BASE_URL (port 8000).
