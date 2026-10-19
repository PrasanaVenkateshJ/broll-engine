# Test pack index: every Submission Form row -> the files that prove it

Row numbers and wording are copied from `testpack/FORM_ROWS.md` (the IncuBrix Excel form). "Met?" is my own
honest answer: **Yes** = evidence in this repo; **Partial** = some evidence, the gap is named; **No** = no evidence yet.

**Main test pack = `run1`** (2026-10-17, all 23 inputs, real adapters: Kokoro, faster-whisper small, Groq
`openai/gpt-oss-120b`, Pixabay, Wikimedia, open_clip, FFmpeg, `STORAGE_BACKEND=local`, my PC:
`testpack/notes/hardware.md`). Numbers come from `testpack/metrics.md` (written by `tools/evaluate.py`).
**`run2`** (2026-10-18) re-ran only 4 inputs after Session 7B (number badges, Openverse, VLM judge,
`QUALITY_MODE=balanced`); see "Run 1 vs run 2" at the end. Videos are git-ignored: `video_pack.zip`
(made by `tools/make_video_pack.py`, with every video's timeline.json / captions.srt / manifest.json + sha256).

## Must-haves (sheet 2_Must_haves, column "Met?")

| Row | Requirement (form wording) | Met? | Evidence |
|---|---|---|---|
| 1 | Both inputs work: audio (mp3, wav, m4a, up to 10 minutes) and text (you generate the narration). | Yes | audio uploads: `outputs/run1/core01-03`, `core11-12`, `hard06` (m4a, longest 293.5 s), `hard07-08` (wav, refused for the right reason); text: `run1/core04-09`, `core13`, `hard01-04`; the 10-minute limit and mp3/wav/URL input: `tests/unit/test_intake.py`. **Live on Modal, 2026-10-18:** a 587 s (9.8 min) narration (`testpack/inputs/hard09_ten_minute.m4a`) succeeded in **1,747.57 s**: 123 scenes, 107 video + 16 photo-motion, 0 repeated clips, QA problems none, audio/video drift 0.03 s (`testpack/live/hardcases_2026-10-18/` `rest/summary.csv`, `rest/hard09_ten_minute/{manifest,timeline}.json`). Caveat: Groq hit its rate limit (HTTP 429, waited 110 s) so scenes 42-122 used the offline rules instead (12 warnings in the manifest) |
| 2 | Scenes by meaning, timed to the voice. Scene changes fall on sentence or phrase boundaries; every second of narration is covered; the narration sets the timing, not the clips. | Yes | every `run1/*/timeline.json` (scene[0].start = 0, scene[i+1].start = scene[i].end, words with times), `captions.srt`; tests `tests/unit/test_scenes.py` |
| 3 | Every clip is licence-clean. Source, licence, attribution and link are recorded per clip; all allow commercial use; no watermarked footage; AI-generated clips are flagged. | Yes | per scene in `run1/*/timeline.json`: `provider, clip_id, page_url, licence, licence_url, attribution, commercial_use, watermark, ai_generated`; `rights` in `manifest.json`; source rules in `SOURCES.md` (Openverse licence filter: `tests/unit/test_footage_quality.py`) |
| 4 | Correct, consistent video. All requested aspect ratios; every clip normalised (resolution, frame rate, pixel format); no black frames, frozen frames or audio drift. | Yes | `run1/core04_*`, `core06_*`, `core10_*` (16:9 + 9:16 + 1:1); `qa` (blackdetect, freezedetect, drift) in every `run1/*/manifest.json`; videos in `video_pack.zip` |
| 5 | No repeats and no nonsense matches. No clip is used twice in one video; footage matches the meaning, not just a keyword. | Partial | repeats: `metrics.md` section 4 (0.0%, 0 of 163 stock scenes); meaning: `run1/hard03_double_meanings/timeline.json` (bank / cloud / mouse / python / value read in the right sense); my ratings: before 3.35 (80 scenes, 25 rated 1-2, `ratings/ratings_before.csv`), after the fixes 3.92 (run3, 66 scenes, 10 rated 1-2, `ratings/ratings_run3.csv`; `metrics.md` sections 7-8). Gap: story moments and lines naming specific items still match loosely |
| 6 | Safe output. No AI-generated realistic faces of real people; no invented brand logos; respectful footage for sensitive topics. | Yes | no generative image/video model is used (cards are FFmpeg text); `run1/hard02_*` (6 warnings, names removed from queries, logo-rejected clip); `run1/hard04_*` (calm queries); `run2/hard04_*` (calm rules for the WHOLE sad story, `story_sensitive` per scene) |
| 7 | Resilient jobs. Jobs run in the background with progress; if one footage source fails, the job still finishes using another source or a fallback. | Yes | every `run1/*/job.json` (status + progress); `testpack/resilience/README.md` (a) source switched off mid-job, (b) corrupted download, (c) two users, (d) Groq outage, all passed |
| 8 | Integration Standard followed and the live demo works for the whole review period. | Partial | Integration rows below; live demo redeployed 2026-10-18 and checked from outside: first /health after idle 6.3 s (cold start), full job succeeded in 59.99 s for 8.75 s of narration (0 warnings), 7 files downloaded, Range request 206 -> ALL CHECKS PASSED (`testpack/live/live_check_2026-10-18.txt`, `live_check_2026-10-18_cold.txt`, `modal_deploy_2026-10-18.log`); earlier check `live_check_2026-10-16.txt`; cost guards + estimate `testpack/notes/cost_modal.md` section 8. Hard-case battery run live on the same deployment (`testpack/live/hardcases_2026-10-18/`): 7 of 9 jobs succeeded, 2 failed and 2 were refused with the expected error codes (see the Hard-case rows). Limits now 20 jobs/day, 50/month, 600 s audio, function timeout 3 h (`cost_modal.md` section 9). Gap: "the whole review period" can only be shown during the review (scale-to-zero, ~$1/month credit) |

## Levels (sheet 3_Levels)

| Level | Requirement (form wording) | Met? | Evidence |
|---|---|---|---|
| Core | All must-haves pass. | Partial | see Must-have 5 and 8 (loose matches, live period) |
| Core | 10 varied inputs (for example a tutorial, a news summary, a product promo, a story; different accents; 20 seconds to 5 minutes) all produce usable videos. | Yes | `run1/summary.md`: 19/19 videos made (tutorial `core04`, news `core05`, promo `core06`, story `core07`, my own voice `core01-03`, Hindi `core11-13`, 15.6 s to 293.5 s); `metrics.md` section 3 |
| Core | Average relevance of at least 3.5 out of 5 from our reviewers on your 10 test videos. | Partial (my own ratings: 3.92) | decided by the reviewers; my own average went from 3.35 (80 scenes, run1 + run2) to 3.92 (66 scenes, run3, 8 videos) (`metrics.md` section 8, `ratings/ratings_run3.csv`) |
| Advanced | At least two footage sources (for example a stock library and a generative video or image-to-video model) with a written, measurable rule for choosing between them, and the reason for each choice stored in timeline.json. | Yes | rule in `app/pipeline/rank.py` docstring + `CLAUDE.md`; `reason` + `relevance_score` per scene in `run1/*/timeline.json`; sources used: pixabay_video, pixabay_image_motion, wikimedia, generated (`metrics.md` section 4) |
| Advanced | Swap a clip: the user replaces one scene's clip and only the affected part is re-rendered, in clearly less time than a full render. | Partial | `tests/integration/test_swap.py`; real timing from an earlier step in `AI_USE.md` (full render 108.9 s vs swap 23.5 s; Studio 114 s vs 22 s). Gap: not re-measured in this test pack |
| Advanced | Looks like one video: colour and brightness normalised across sources; sensible pacing (no flashing 0.5-second clips). | Yes | colour/brightness: `TARGET_LUMA` / `TARGET_SATURATION` in every `run1/*/manifest.json` `settings`; pacing: shortest scene of all 184 run1 scenes is 2.1 s (`timeline.json` `duration`) |
| Advanced | Accepts IncuBrix scene lists (text plus visual hint per scene). | Yes | `run1/core10_scene_list_incubrix`, `run1/hard05_*` (`visual_hint` is the first query) |
| Outstanding | Measured relevance: an independent vision-language model scores each clip against its narration, and you show how well that score agrees with human ratings. | Partial | judge `app/pipeline/vlm.py` (Groq `qwen/qwen3.8-27b`, a Groq preview model); `vlm_score` per judged scene in `run2/*/timeline.json`; agreement with my ratings: before 5 scenes with both (Spearman 0.89), run3 7 scenes (Spearman 0.18) (`metrics.md` sections 7-8). Gap: too few scenes, and no clear agreement yet (the judge rates only uncertain scenes, free-tier limit) |
| Outstanding | Multilingual narration: Hindi and Tamil voiceovers work as well as English. | Partial | `run1/core03_*` (my Tamil voice), `run1/core11-13` + `run2/core11_*` (Hindi): Tamil / Devanagari captions, English search queries; number words in Hindi and Tamil: `tests/unit/test_cards.py`. My ratings before -> run3 (`metrics.md` sections 2 + 8): English 3.34 -> 3.94, Hindi 4.22 -> 4.38, Tamil 2.17 -> 3.00 (per-scene translation). Gap: Tamil is still below English (a Whisper transcription error in one scene) |
| Outstanding | Production numbers: time and cost to produce one minute of finished video, and how many jobs one server handles at once. | Yes | `metrics.md` section 5 (turnaround cold / warm, time per minute, cost per minute, 1 / 2 / 3 jobs at once from `outputs/concurrency/results.json`); Modal cost = estimate (`notes/cost_modal.md`) |
| Outstanding | At least 85% of scenes rated 4 or 5 out of 5 by our reviewers. | No (my own ratings: 72.7%) | decided by the reviewers; I rated 48.8% of 80 scenes 4-5 before and 72.7% of 66 scenes in run3 (`metrics.md` section 8) |

## Integration Standard (sheet 4_Integration)

| Row | Requirement (form wording) | Met? | Evidence |
|---|---|---|---|
| 1. One-command start and test | docker compose up starts the service and everything it needs (database, storage, local test servers). One command runs all tests (unit, integration and at least one end-to-end) with no API keys. A .env.example lists every setting. | Yes | `docker-compose.yml`; `tests/` (all mock): 467 tests, 465 passed, 2 skipped (Playwright, MinIO-only) on 2026-10-18 (`logs/pytest_session7b.xml`); `.env.example` checked by `tests/unit/test_docs_in_sync.py`. Docker was not re-run in this session |
| 2. Documented HTTP API | An openapi.yaml (OpenAPI 3.1) that matches the running service exactly. All routes under /api/v1/. | Yes | `openapi.yaml`, `tests/unit/test_openapi_contract.py` |
| 3. Background jobs | Anything slower than 2 seconds runs as a job: POST returns 202 with a job_id; GET /api/v1/jobs/{job_id} returns status (queued, running, succeeded, failed), progress 0-100 and result links. Optional callback URL, signed with HMAC-SHA256 in an X-IncuBrix-Signature header. | Yes | every `run1/*/job.json`; `logs/run1_batch.log`; callback signature: `tests/integration/test_job_flow.py`, `tests/unit/test_jobs.py` |
| 4. Safe retries | An Idempotency-Key header on every POST: sending the same request twice with the same key returns the same job and never does the work twice. | Yes | `tools/batch_run.py` (key from the input); re-runs needed NEW input names (`perf/inputs_warm/`) because the same input replays the same job; `tests/unit/test_idempotency.py` |
| 5. Clear errors | Errors are JSON: {"error": {"code", "message", "retryable", "details"}}. Codes are UPPER_SNAKE_CASE and listed in the README. | Yes | `run1/hard07-10/job.json`; README error table (checked by tests) |
| 6. Configuration | All settings, paths, model names and limits come from environment variables or a config file. No hard-coded paths or keys. | Yes | `app/config.py`, `.env.example`, README settings table (new: `QUALITY_MODE`, `EARLY_EXIT`, `VLM_*`, `OPENVERSE_API_URL` ...); `quality_mode` + `settings` in every `run2/*/manifest.json` |
| 7. Files in, files out | Inputs accepted as an upload or a URL. Outputs saved to S3-compatible storage (MinIO in the compose file is fine) and returned as download links. | Yes | uploads: `run1/core01-03`; URL input: `tests/unit/test_intake.py`; download links: `logs/run1_batch.log` (local backend in this pack; MinIO test runs in Docker/CI) |
| 8. Logs and health | Structured JSON logs with timestamp, level, job_id and the X-Correlation-Id from the request. GET /health and GET /ready endpoints. | Yes | `logs/worker_*.log`, `logs/api_*.log` (`job_id`, `correlation_id`); `/health`, `/ready` in `live/live_check_2026-10-16.txt` |
| 9. Output manifest | Every result includes a JSON manifest: which models/providers and versions were used, settings, seed, timings, cost estimate, warnings, and licence/rights information. | Yes | every `run1/*/manifest.json` and `run2/*/manifest.json` |
| 10. Batch runner | A command-line tool that runs a folder or file of test inputs against the API and writes all results to one folder. We use this to test every submission the same way. | Yes | `tools/batch_run.py`, `outputs/run1/summary.csv`, `summary.md` |

## Metrics (sheet 5_Metrics)

| Metric | Form wording | Met? | Evidence |
|---|---|---|---|
| Turnaround | Time from upload to finished video for 60 s of narration, cold and warm. | Yes | `metrics.md` section 5 (run1: cold 328.8 s, warm 280.6 s / 256.6 s, `outputs/perf/`); run2 cold 339.7 s (`outputs/run2/perf_a_gardening`) |
| Cost | Cost per minute of finished video (footage APIs, generation, TTS, compute). | Yes | `metrics.md` section 5 (local $0; Modal estimate from `notes/cost_modal.md`) |
| Coverage | Share of narration time covered by a clip you rate 3 or more out of 5. | Yes | rated narration time with a clip I rated 3+: 69.4% before (80 scenes), 84.1% in run3 (66 scenes) (`metrics.md` sections 1 + 8) |
| Relevance | Your own 1-5 rating per scene (and the independent model score for Outstanding). | Yes | my ratings: before average 3.35 (counts 4/21/16/21/18, `ratings/ratings_before.csv`), run3 average 3.92 (counts 4/6/8/21/27, `ratings/ratings_run3.csv`) (`metrics.md` sections 1, 2, 7, 8; `metrics_run3.md`); model score: `vlm_score` in `run2/*` and `run3/*/timeline.json` |
| Reliability | Render success rate across all tests; repeat-clip rate (must be 0). | Yes | `metrics.md` sections 3-4: 19/19 renders, 23/23 expected outcomes, repeat rate 0.0% |
| Resources | Peak memory and GPU time, if you used a GPU. | Yes | `metrics.md` section 5 (worker peak 4264 MB; `logs/memory_*.csv`, `logs/peaks_*.txt`); no GPU used |

## Hard cases (sheet 6_Hard_cases)

| Row | Hard case (form wording) | Met? | Evidence + result |
|---|---|---|---|
| 1 | Abstract lines ("trust is earned slowly") and numbers or statistics ("sales grew 40% in 2025"). | Yes | `run1/hard01_*`: "40%" number card, "trust" / "hope" keyword cards; `run2/hard01_*`: the numbers drawn as badges on real footage (`overlay`), abstract lines searched with concrete metaphors. **Live on Modal, 2026-10-19** (`testpack/live/hardcases_2026-10-19/` `summary.csv`): `hard01_abstract_and_statistics` succeeded in 168.95 s (6 scenes, 0 warnings, 0 plain cards, 0 QA problems): "40%" and "3 out of 4" drawn as badges on real footage; abstract lines searched as concrete scenes ("handshake between colleagues", "person meditating by window", "cozy startup workspace"); "fell by half" has no digit figure so it got footage without a badge. The live 10-minute job (2026-10-18) also had 12 abstract lines and 0 plain cards. Live clips were not rated |
| 2 | A real, named person or brand mentioned in the narration. | Yes | `run1/hard02_*`: 6 warnings, generic queries, logo-rejected clip. **Live:** `rest/hard02_named_person_and_brand`: succeeded in 78.5 s, 5 warnings (named people Satya Nadella / Sachin Tendulkar, brands Microsoft / Coca-Cola: "generic footage without logos / no faces"), queries generic ("corporate leader in office", "large cricket stadium", "international airport duty-free"), logo check ok on all 4 clips |
| 3 | Words with double meanings: value, bank, cloud, mouse, python. | Yes | `run1/hard03_*/timeline.json`: every sense right. **Live:** `rest/hard03_double_meanings`: succeeded in 125.3 s, 0 warnings; queries read from `timeline.json`: bank = "people sitting by riverbank" / "interior of a bank", mouse = "mouse like tiny explorer" (animal) / "hand clicking computer mouse", python = "python snake coiled on branch" / "programmer writing code on laptop"; cloud and value were searched as metaphors ("clouds rolling like waves", "files floating like balloons", "hands exchanging glowing heart"), weaker than the others. Clips of this live run were not rated |
| 4 | A sad or sensitive news story. | Yes | `run1/hard04_*` (sensitive lines calm); `run2/hard04_*` (the whole story gets calm rules, `story_sensitive` on every scene). **Live on Modal, 2026-10-19** (`testpack/live/hardcases_2026-10-19/` `hard04_sensitive_news`): succeeded in 142.21 s, `story_sensitive` true on all 6 scenes, 0 plain cards, 0 QA problems; queries name the real subject without a "calm" prefix ("flooded neighborhood with empty homes", "rescue workers arriving in village", "doctor examining child", "volunteers sorting supplies", "candles lit in memory"); 2 warnings, both logo-rejected candidates replaced by the next one; scene 2 used the best-available rule (score 0.212 < 0.22) with a keyword badge. Live clips were not rated |
| 5 | A 9:16 output where the important subject sits at the edge of a 16:9 clip. | Partial | `run1/hard05_*/crop_check/README.md` (1 correct, 2 not real edge tests, 1 weakness: a near-tie crop cut the boat); fix + test with that real case: `tests/unit/test_footage_quality.py::test_the_sailboat_near_tie_keeps_the_boat_whole` (not re-run as a video). **Live on Modal, 2026-10-19** (`testpack/live/hardcases_2026-10-19/` `hard05_crop_subject_at_edge_9x16`): the job succeeded in 78.17 s with 0 QA problems in 16:9 and 9:16, and the crop log shows the near-tie rule working (scenes 2 and 3: "'centre' was a near-tie but its edge falls inside the subject"). **But the picture is not right:** in scene 2 (sailboat at x = 0.60-0.72 of the frame) the 9:16 window chosen is the left third (0-0.37), so the sailboat is cropped out and the 9:16 frame shows only empty haze; in scene 3 the cyclist's face is only partly in the centre window. The rule avoids cutting through the subject but can pick a window that leaves it out. Checked by looking at the 9:16 frames of scenes 2 and 3 (mid-scene frames, not committed). Not fixed |
| 6 | A 5-minute voiceover; silent audio; music-only audio; a wrong file type; an empty script. | Yes | `run1/hard06_*` (293.5 s, 61 scenes), `hard07` NO_SPEECH_DETECTED, `hard08` MUSIC_ONLY_AUDIO, `hard09` UNSUPPORTED_FILE_TYPE, `hard10` EMPTY_SCRIPT. **Live on Modal** (`testpack/live/hardcases_2026-10-18/` `rest/summary.csv`): 9.8-minute voiceover **succeeded in 1,747.57 s** (see Must-have 1; a 5-minute file was not repeated live to save credit); silent audio -> `NO_SPEECH_DETECTED` after 7.6 s; music-only -> `MUSIC_ONLY_AUDIO` after 11.3 s; text file renamed .mp3 -> `UNSUPPORTED_FILE_TYPE` (refused at once); empty script -> `EMPTY_SCRIPT` (refused at once) |
| 7 | A footage provider that is down or returns nothing; a corrupted download; two users submitting at the same time. | Yes | `testpack/resilience/README.md` (a)-(d), `outputs/resilience/`; `outputs/concurrency/results.json`. **Live on Modal:** two jobs posted 15 ms apart (`pair/summary.csv`: `aa_sim_a`, `aa_sim_b`, created 16:00:25.98 and 16:00:25.99 UTC) both succeeded (67.9 s and 99.9 s, 0 warnings, 0 QA problems, same single-token user, one worker so the second queued behind the first). Live provider trouble came up on its own in the 10-minute job: Openverse timed out after 15 s and the job used the next source, and 4+ clips rejected by the frame logo check were replaced by the next candidate (warnings in its manifest). A provider that is switched off and a corrupted download were not forced live (local tests only) |

## Where things are

| What | Path |
|---|---|
| Report (2-4 pages), licences, README | `REPORT.md`, `SOURCES.md`, `README.md` |
| Inputs (23) + what each tests | `testpack/inputs/`, `testpack/inputs/README.md`, expected outcomes `testpack/expected.json`. My own voice recordings (`core01`-`core03` `*_myvoice.m4a` and `inputs/my_voice/`) are NOT in the repo, for privacy (`.gitignore`); their outputs (timeline, captions, manifest) are in `run1/core01-03` |
| Rating page (run3: 66 scenes, 8 videos) | `testpack/ratings/rate.html` (made by `tools/make_rating_page.py --runs testpack/outputs/run3`; English meaning under Hindi / Tamil scenes; videos play from the local git-ignored outputs). My earlier 80 ratings (run1 + run2) are kept in `testpack/ratings/ratings_before.csv` |
| Run 3 (fixes from my ratings: per-scene translation, best-effort footage instead of plain cards, no "calm" prefix, one rewritten query for borderline scenes) | `testpack/outputs/run3/` (the 8 inputs behind the 10 rated jobs; `summary.md`, `logs/run3_batch.log`) |
| Main run | `testpack/outputs/run1/` |
| Session 7B re-run (4 inputs) | `testpack/outputs/run2/` (`summary_60s.*` = the 60 s job, `summary.*` = the other 3) |
| Production runs | `testpack/outputs/perf/` (run1), `outputs/perf_run2/` (first Session 7B try with the full search = today's `QUALITY_MODE=best`, kept for the record; `warm_new_script` was stopped half-way and is not used) |
| Logs, memory samples | `testpack/logs/` |
| Ratings, metrics | `testpack/ratings/rate.html`, `testpack/ratings/ratings_before.csv` (my 80 ratings of run1 + run2), `testpack/ratings/ratings_run3.csv` (my 66 ratings of run3), `testpack/metrics.md`, `testpack/metrics_run3.md` |

## Fresh unseen scripts

3 new scripts (`testpack/fresh/inputs/`), normal pipeline, no code changes before the run: `testpack/outputs/fresh/`,
rating page `testpack/ratings/rate_fresh.html`, my ratings `testpack/ratings/ratings_fresh.csv`, details in
`testpack/metrics.md` section 9 (+ `testpack/metrics_fresh.md`).

- Average relevance **2.67** (run3 on the tuned inputs: 3.92); **44.4%** rated 4-5 (4 of 9); coverage 51.6%; 0 plain cards.
- Rated 1-2: fresh01 scene 1 (chai making -> "stovetop kettle on kitchen counter"), fresh02 scene 0 (evaporation ->
  "wide ocean horizon"), fresh02 scene 2 (rain falling -> "water droplets like pearls"), fresh03 scene 0 (cyber fraud ->
  "home office laptop screen"): generic setting / metaphor clips instead of the line's main action.
- "one in five" got no badge; fixed in `cards.py` ("X in Y" ratios) and fresh03 re-run: the "1 in 5" badge now shows.
  The rated fresh03 video is kept in `testpack/outputs/fresh_rated_v1/`.

## Run 1 vs run 2 (the 4 re-run inputs)
| Input | Turnaround s run1 -> run2 | Footage stage s | Candidates scored | Plain cards (number + keyword) | video / photo-motion | Number badges | Judge rated / mean | Groq fallback scenes | Mode |
|---|---|---|---|---|---|---|---|---|---|
| perf_a_gardening (60 s English, cold) | 324.9 -> 327.1 | 132.2 -> 112.1 | not recorded -> 150 | 0 -> 0 | 12/0 -> 8/4 | 0 -> 0 | 0 / - -> 0 / - | 0 -> 0 | balanced |
| core11_hi_tts_saving (Hindi) | 118.5 -> 137.5 | 47.8 -> 52.7 | not recorded -> 87 | 0 -> 0 | 3/1 -> 3/1 | 0 -> 0 | 0 / - -> 1 / 4.00 | 0 -> 0 | balanced |
| hard01_abstract_and_statistics (numbers) | 98.7 -> 142.5 | 50.6 -> 72.4 | not recorded -> 132 | 3 -> 0 | 2/1 -> 5/1 | 0 -> 2 | 0 / - -> 2 / 4.50 | 0 -> 0 | balanced |
| hard04_sensitive_news (hard case) | 144.3 -> 146.8 | 56.8 -> 59.8 | not recorded -> 125 | 1 -> 0 | 3/2 -> 3/3 | 0 -> 0 | 0 / - -> 2 / 3.50 | 0 -> 0 | balanced |

From `testpack/compare_run1_run2.md` (written by `tools/compare_runs.py --pair ...` from the job folders). Run 1 of
the 60 s input is `outputs/perf/cold_60s` (the same input, cold). Turnaround here = job created -> finished
(server side, from job.json); the batch client, which also counts upload + download, measured 328.8 s (run1)
and 339.7 s (run2) for the 60 s job. Run 1 did not record how many candidates it scored. Run 2 = one run only,
`QUALITY_MODE=balanced`, judge on (`VLM_PROVIDER=groq`), laptop on charger + Best performance.

- 60 s narration: about the same time as run 1 (327.1 s vs 324.9 s). The footage stage got faster (112 s vs
  132 s, 150 candidates scored instead of 720 in the first full Session 7B try) and render got faster (105 s vs
  127 s), but TTS (65 s vs 40 s) and transcription (35 s vs 21 s), which did not change, were slower on this
  laptop on this day. Video share on this input went from 12/12 to 8/12 (4 photo-motion scenes).
- Plain cards in these 4 inputs: 4 -> 0. hard01's "40%" and "3 out of 4" are now badges on real footage.
- hard04: every scene has `story_sensitive: true` (calm rules for the whole sad story).
- Groq understanding in batches of 6: no scene fell back to the offline rules. The judge got one HTTP 429 in the
  60 s job and was skipped at once (0 s waited).
- Peak memory during run 2 (`logs/memory_run2b.csv`): worker 3,259 MB, API + worker + ffmpeg 5,004 MB at one
  moment (3 segments rendered in parallel here; run 1's highest sample was 5,148 MB).
