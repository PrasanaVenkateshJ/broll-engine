# Test pack index: every requirement -> the files that prove it

All evidence was produced on 2026-10-17 on my PC (`testpack/notes/hardware.md`) with the real adapters from my
`.env` (Kokoro, faster-whisper small, Groq `openai/gpt-oss-120b`, Pixabay, Wikimedia, open_clip, FFmpeg,
`STORAGE_BACKEND=local`). Numbers come only from `testpack/metrics.md` (written by `tools/evaluate.py` from the
files below). Videos are git-ignored: they are in `video_pack.zip` (made by `tools/make_video_pack.py`, outside
the repo, with every video's timeline.json / captions.srt / manifest.json and a sha256 index).

Row numbers follow the "FORM ROWS" lines in `PLAYBOOK.md` (Must-have 1-8, Advanced 1-4, Integration 1-10,
Hard cases 1-7). The form itself is not in the repo, so check each row's wording against the form.

## Where things are

| What | Path |
|---|---|
| Inputs (23) + what each tests, source, rights, length | `testpack/inputs/`, `testpack/inputs/README.md` |
| Expected outcome of every input (written before the run) | `testpack/expected.json` |
| Main run (all inputs, 16:9 + 9:16, 1:1 for 3 inputs) | `testpack/outputs/run1/` (`summary.md`, one folder per input: job.json, timeline.json, manifest.json, captions.srt, thumbnails) |
| Production runs (cold / warm 60 s) | `testpack/outputs/perf/` (+ `cold_a`: first cold try, only 49 s narration) |
| 1 / 2 / 3 jobs at once | `testpack/outputs/concurrency/results.json` (+ `concurrency_attempt1_crashed/`: first try, crashed in my tool) |
| Resilience tests (a)-(d) | `testpack/resilience/README.md`, `testpack/outputs/resilience/` |
| Logs (API, workers, batch, memory samples, exact peaks) | `testpack/logs/` |
| Rating page / ratings / metrics | `testpack/ratings/rate.html`, `testpack/ratings/ratings.csv` (after rating), `testpack/metrics.md` |
| Tools | `tools/batch_run.py`, `tools/make_test_inputs.py`, `tools/load_test.py`, `tools/resilience.py`, `tools/fault_worker.py`, `tools/sample_memory.ps1`, `tools/process_peaks.ps1`, `tools/make_rating_page.py`, `tools/evaluate.py`, `tools/make_video_pack.py` |

## Must-haves

| Row | Requirement | Evidence |
|---|---|---|
| Must-have 1 | Input: voiceover upload (mp3/wav/m4a), script (TTS), scene list; bad input refused | uploads: `run1/core01-03, core11-12, hard06` (m4a); scripts: `run1/core04-09, core13, hard01-04`; scene lists: `run1/core10, hard05`; refusals: `run1/hard09_*` (UNSUPPORTED_FILE_TYPE), `run1/hard10_*` (EMPTY_SCRIPT). URL input: tests only (`tests/unit/test_intake.py`), not in this pack |
| Must-have 2 | Transcription + contiguous scenes + captions | every `run1/*/timeline.json` (scene[0].start = 0, no gaps; words with times), `captions.srt`; speech checks: `run1/hard07_*` NO_SPEECH_DETECTED, `run1/hard08_*` MUSIC_ONLY_AUDIO |
| Must-have 3 | Footage found per scene, selection rule + reason stored | `timeline.json` fields `provider, clip_id, reason, relevance_score, queries`; mix in `metrics.md` section 4 |
| Must-have 4 | Outputs: 3 ratios, H.264/AAC 30 fps yuv420p, srt, timeline, manifest | `run1/core04_*`, `core06_*`, `core10_*` (16:9 + 9:16 + 1:1); videos in `video_pack.zip`; manifest fields: `run1/*/manifest.json` |
| Must-have 5 | Queries mean the SENSE of the line | `run1/hard03_double_meanings/timeline.json` (all 9 senses right), `run1/*/timeline.json` `queries`; human ratings: `metrics.md` section 1 |
| Must-have 6 | Safety: named people / brands / sensitive / avoid | `run1/hard02_*` (warnings, names removed from queries, no-face excludes in `safety`), `run1/hard04_*` (calm queries, graphic terms excluded) |
| Must-have 7 | No repeated clip in a video; licences recorded | `metrics.md` section 4 (repeat rate 0.0%, 0 of 163 stock scenes); `licence, licence_url, attribution, commercial_use, watermark` per scene in `timeline.json` |
| Must-have 8 | Live demo | not part of this local pack (Modal, `deploy/modal/`, `testpack/live/`); cost estimate `testpack/notes/cost_modal.md` |

## Advanced

| Row | Requirement | Evidence |
|---|---|---|
| Advanced 1 | Provider fallback / selection rule | `resilience/a_switch_off` (pixabay_video off mid-job -> pixabay_image_motion), `run1/*/timeline.json` reasons (`below 0.22`, logo rejections, `generated` fallback) |
| Advanced 2 | Clip swap re-renders one scene | not re-measured in this pack (tests `tests/integration/test_swap.py`; earlier real timing in `AI_USE.md`) |
| Advanced 3 | 9:16 crop around the subject | `run1/hard05_crop_subject_at_edge_9x16/crop_check/README.md` + frames (`crop` per segment in timeline.json). Honest result: 1 correct, 2 not real edge tests, 1 weakness (16:9 crop of a portrait clip cut the boat) |
| Advanced 4 | Scene lists with visual hints | `run1/core10_scene_list_incubrix`, `run1/hard05_*` (`visual_hint` = first query) |

## Integration standard

| Row | Point | Evidence |
|---|---|---|
| Integration 1 | compose + tests with no keys | `tests/` (all mock): 392 passed, 2 skipped on 2026-10-17 (22 new tests in this step); Docker not run in this step |
| Integration 2 | OpenAPI matches | `openapi.yaml`, `tests/unit/test_openapi_contract.py` (no API change in this step) |
| Integration 3 | Jobs: 202 + status + progress | every `run1/*/job.json`; `logs/run1_batch.log` |
| Integration 4 | Idempotency-Key | `tools/batch_run.py` keys from the input; re-runs needed NEW names (`perf/inputs_warm/`, `resilience/inputs/res_a_switch_off_run2.json`, `--tag _r2` in the load test) because the same input replays the same job |
| Integration 5 | Error format + codes | `run1/hard07-10/job.json` (`{"error": {code, message, retryable, details}}`), `resilience/c_two_users/events.json` (404 JOB_NOT_FOUND) |
| Integration 6 | Settings from env | new settings `CAPTION_FONTS_BY_LANGUAGE`, `CAPTION_FONT_SCALES`, `CAPTION_FONTS_DIR`, `TRANSLATE_FOR_SEARCH` in `app/config.py`, `.env.example`, README table; overrides used in tests via env (`WORKER_CONCURRENCY`, `GROQ_API_KEY`, `FOOTAGE_CACHE_SECONDS`, `API_TOKENS`) |
| Integration 7 | Outputs to storage, signed links | `logs/run1_batch.log` + every downloaded file (local storage backend) |
| Integration 8 | JSON logs with job_id + correlation id | `logs/worker_*.log` (`"job_id"`, `"correlation_id": "batch-<input>"`) |
| Integration 9 | manifest.json content | `run1/*/manifest.json` (models + versions, settings, seed, timings, cost, warnings, rights, ai_generated, footage_mix, translate_for_search) |
| Integration 10 | Batch runner | `tools/batch_run.py`, `outputs/run1/summary.csv`, `summary.md` |

## Hard cases

| Row | Hard case | Input | Evidence + result |
|---|---|---|---|
| Hard case 1 | abstract lines + statistics | `hard01_abstract_and_statistics.json` | `run1/hard01_*/timeline.json`: "40%" number card; "trust"/"hope" keyword cards (no stock clip passed 0.22); "fell by half" / "three out of four" flagged statistic but got stock footage (no digit figure) |
| Hard case 2 | named person + brand | `hard02_named_person_and_brand.json` | `run1/hard02_*`: 6 warnings, generic queries (office building, stadium, supermarket aisle), logo-rejected clip in scene 3 |
| Hard case 3 | double meanings | `hard03_double_meanings.json` | `run1/hard03_*/timeline.json`: bank / cloud / mouse / python / value all read in the right sense |
| Hard case 4 | sad / sensitive news | `hard04_sensitive_news.json` | `run1/hard04_*`: scenes 0, 1, 5 flagged sensitive with "calm" queries; scene 3 (doctors and children) not flagged (flag is per line) |
| Hard case 5 | 9:16 subject at the edge | `hard05_crop_subject_at_edge_9x16.json` | `run1/hard05_*/crop_check/README.md` (decision + frames per scene) |
| Hard case 6 | 5-min voiceover, silent, music-only, wrong type, empty script | `hard06`-`hard10` | `run1/hard06_*` (293.5 s, 61 scenes, succeeded; Groq 429 -> rules fallback), `hard07` NO_SPEECH_DETECTED, `hard08` MUSIC_ONLY_AUDIO, `hard09` UNSUPPORTED_FILE_TYPE, `hard10` EMPTY_SCRIPT |
| Hard case 7 | provider down, corrupted download, two users, (Groq outage) | `testpack/resilience/inputs/` | `testpack/resilience/README.md`: (a) switch-off, (b) corrupted download, (c) two users, (d) Groq outage - all passed |
| Languages | Tamil + Hindi: English search, original captions | `core03` (my Tamil), `core11-13` (Hindi TTS) | `run1/core03_*`, `core11-13`: `language` ta / hi, `search_text` (Whisper translation) + English `queries`, Tamil / Devanagari `captions.srt`, `caption_font` Noto Sans Tamil / Devanagari per segment; tests `tests/unit/test_multilingual.py`, `tests/integration/test_multilingual_job.py`, `tests/unit/test_caption_fonts.py` |

## Metrics (all in `testpack/metrics.md`)

| Metric | Source |
|---|---|
| Average relevance, % rated 4-5, coverage >= 3, Spearman CLIP vs rating, per language | `testpack/ratings/ratings.csv` (export from `rate.html`) - "not measured" until I rate the 184 scenes |
| Repeat-clip rate, render success, expected outcomes, footage mix | `testpack/outputs/run1/*/timeline.json`, `job.json`, `summary.csv`, `testpack/expected.json` |
| Turnaround cold / warm for 60 s | `testpack/outputs/perf/cold_60s`, `warm_same_script`, `warm_new_script` |
| Time per minute of finished video | `testpack/outputs/run1/summary.csv` |
| Peak memory of API + worker | `testpack/logs/memory_*.csv`, `testpack/logs/peaks_*.txt` |
| Jobs at once (1 / 2 / 3) | `testpack/outputs/concurrency/results.json` |
| Cost per minute | `testpack/metrics.md` section 5 (local $0; Modal = estimate from `testpack/notes/cost_modal.md`) |
| Hardware | `testpack/notes/hardware.md` |
