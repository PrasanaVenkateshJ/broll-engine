# B-roll Engine: report (IncuBrix Assessment 3)

Every number below comes from a file in `testpack/` (paths in brackets). Nothing here was measured by anyone else:
the relevance ratings are **my own** 1-5 ratings, not the reviewers'.

## 1. The problem

A narration goes in (a voiceover file, a plain script that we voice, or an IncuBrix scene list). B-roll videos come
out (16:9, 9:16, optionally 1:1) where every scene has a clip that matches what is being said, with captions,
a `timeline.json` that explains every choice, and a `manifest.json` with models, settings, timings and licences.
Hard parts: footage must match the *meaning* (abstract lines, numbers, double meanings), every clip must be
licence-clean and unique, sad stories must be handled respectfully, and it must run as a background job on a
small server.

## 2. Architecture

```
client -> API (FastAPI, 202 + job_id) -> SQLite job row -> worker (separate process, atomic claim)
worker: intake -> TTS (Kokoro) -> transcribe (faster-whisper) -> scenes -> understand (Groq or rules + safety)
        -> footage (Pixabay video, Pixabay photo, Wikimedia, Openverse, generated card; CLIP scoring)
        -> render each scene x ratio (FFmpeg) + QA -> timeline.json, manifest.json -> storage -> signed links
```

- **Narration sets the timing.** Scenes are contiguous, cut at sentence ends, then commas, then pauses; 2-8 s.
- **Understanding** (Groq `openai/gpt-oss-120b`, offline rules as fallback per scene): search queries, flags
  (abstract, statistic, named person, brand, sensitive), and for non-English narration an English translation of
  each scene's own text.
- **Selection rule, measurable and stored per scene:** the best candidate with CLIP score `>= 0.22` that is not used
  yet; a stock video beats photo-motion unless the photo is clearly better; a statistic line gets real footage with
  its number drawn as a badge (the number is read from the narration only, so a card can never show an invented
  figure); a near miss (`>= 0.1936`) is used only if an independent vision model rates it 4+; then the best usable
  clip with a keyword badge; a plain FFmpeg card only if no footage is usable.
- **Safety:** no generated faces (no generative model is used at all), names and brands removed from queries with a
  warning, a logo check on the thumbnail and on frames of the downloaded clip, graphic words excluded for sensitive
  topics, festive footage also excluded when the whole story is sad.
- **Licences:** only Pixabay Content License, CC0, Public Domain and CC BY (with a named creator); everything else is
  rejected in code. Source, licence, link and attribution are stored per clip.
- **Speed work:** parallel scene search, cached searches and TTS, prefetch of the likely winner, early exit, parallel
  segment rendering, and `QUALITY_MODE` (`fast` / `balanced` / `best`).
- **Integration standard:** OpenAPI 3.1 matched by a contract test, idempotency keys, HMAC-signed callbacks, JSON
  errors with UPPER_SNAKE codes, JSON logs with correlation ids, `/health` and `/ready`, batch runner, signed
  Range-capable downloads, a web Studio, and clip swap that re-renders only one scene.

## 3. Key decisions (and why)

| Decision | Why |
|---|---|
| Stock footage + CLIP ranking, no generative video | Licence-clean, no AI faces, free; `ai_generated` is false for every clip. |
| Rules as the selection logic, models only as inputs | Anyone can re-check a choice from `timeline.json` (score, threshold, reason). |
| Number badges on real footage instead of plain number cards | My ratings: plain cards averaged 1.88 of 5 (8 cards); stock video and photo-motion about 3.5. |
| Groq in batches of 6 scenes, 429 handling, per-scene rules fallback | With 12 scenes per request the answer was cut short in 2 of 3 real runs. |
| Each Hindi / Tamil scene translated on its own | A whole-audio translation split by time shifted words into the neighbouring scene (Tamil 2.17 -> 3.00). |
| Removed the "calm" prefix from sad-story queries | It made searches generic (hard04 fell from 3.50 to 2.33); the graphic / festive filters stay. |
| Vision judge optional and limited | Free tier: 8,000 tokens/min, preview model; so one contact sheet per scene, at most 3 scenes per job, never waits on a 429. |

## 4. Hosting story

1. **Hugging Face Docker Space** was built first (`deploy/hf-space/`), but Hugging Face made Docker Spaces paid in
   July 2026, so it is not used.
2. **Azure** was evaluated and rejected.
3. **Modal** (free monthly credit, about $1 on this account) hosts the live demo: one container (2 cores, 5 GiB request /
   8 GiB limit), scale to zero, models baked into the image, SQLite + outputs on a Volume, cost guards (12 jobs/day,
   35/month, 300 s narration, rate limits). A Cloudflare tunnel script is the no-account fallback.
   Measured on Modal after the 2026-10-18 redeploy [`testpack/live/live_check_2026-10-18.txt`,
   `live_check_2026-10-18_cold.txt`]: the first `/health` after idle took **6.3 s** (cold start; `/ready` 6.9 s in a second
   cold test), a full job (23 words, 8.75 s of narration, 2 scenes, 16:9 + 9:16) **succeeded in 59.99 s with 0 warnings**
   (TTS 17.3, transcribe 5.7, understand 2.2, footage 15.1, render 15.2 s), all 7 output files downloaded, and a Range
   request returned **HTTP 206** (`Content-Range bytes 0-1023/2727960`). All checks passed. One short job on 2 cores
   only; the 2026-10-16 job on the old code took 44 s [`live_check_2026-10-16.txt`].

## 5. Measured results (this PC: i7-13620H, 16 GB, CPU only)

**Reliability** [`testpack/metrics.md` sections 3-4]: 23 inputs (3 in my own voice: two English, one Tamil; 3 Hindi; error cases):
19/19 valid inputs produced videos, 23/23 matched the expected outcome (including `NO_SPEECH_DETECTED`,
`MUSIC_ONLY_AUDIO`, `UNSUPPORTED_FILE_TYPE`, `EMPTY_SCRIPT`); repeat-clip rate **0.0%** (0 of 163 stock scenes);
a 293.5 s voiceover with 61 scenes succeeded. Resilience tests (source switched off mid-job, corrupted download,
two users, Groq outage) all passed [`testpack/resilience/README.md`]. 478 automated tests (476 pass, 2 optional skips).

**Relevance (my own ratings)** [`metrics.md` sections 7-9]:

| Set | Scenes | Average (1-5) | Rated 4-5 | Coverage (time with a clip rated 3+) |
|---|---|---|---|---|
| Run 1 + run 2 (before the rating fixes) | 80 | 3.35 | 48.8% | 69.4% |
| Run 3 (after the fixes, same inputs) | 66 | **3.92** | **72.7%** | **84.1%** |
| Fresh unseen scripts (no tuning) | 9 | **2.67** | 44.4% | 51.6% |

Run 3 by language: English 3.94, Hindi 4.38, Tamil 3.00. Same scripts before and after: hard01 (numbers) 2.50 ->
4.33, hard04 (sad story) 3.50 -> 4.33. Plain cards among rated scenes: 8 -> 0.

**Why the fresh scripts scored lower (2.67):** all four low scenes passed the CLIP threshold comfortably (0.27-0.32) but
showed a *setting or metaphor* ("kitchen counter", "ocean horizon", "water droplets like pearls") instead of the line's
**main action**, and each short script became only three long scenes, so one clip had to cover two or three actions.
The run-3 gains were partly tuned on the inputs I rated, so they do not fully carry over. **Planned fix** (designed,
not yet in the code): cut long sentences at clause / action boundaries ("and", "then", commas) into 2.5-5 s scenes,
make the first query the visible action plus subject, count that query in the score, and ask the judge whether the clip
shows the main action.

**Independent vision judge vs my ratings:** only 5 scenes (Spearman 0.89) and 7 scenes (0.18) had both, too few to
claim agreement. CLIP score vs my rating: Spearman 0.289 (n = 72) before, 0.127 (n = 66) after.

**Timings** [`metrics.md` section 5; `testpack/outputs/perf/`]: 60 s of narration (16:9 + 9:16, one job alone): cold
**328.8 s**, warm same script 280.6 s, warm new script 256.6 s; run 2 with `QUALITY_MODE=balanced` 339.7 s (client) /
327.1 s (job), so the quality changes did not make it faster or slower. Median 365 s of processing per narration
minute. Stage split of the cold job: TTS 40, transcribe 21, footage 132, render 127 s. Jobs at once: 1 / 2 / 3 jobs =
11.1 / 14.9 / 13.9 jobs per hour, so one server gains little beyond 2.

**Memory** [`testpack/logs/peaks_*.txt`, `memory_*.csv`]: API about 90 MB; worker (Kokoro + Whisper + CLIP) exact peak
4,109-4,264 MB; worker + ffmpeg at most 4,396 MB with one render worker (5,204 MB with three). Modal is set to 5 GiB /
8 GiB from that. No GPU used.

**Cost:** local $0 (free tiers, CPU). Modal is an **estimate**, not a measurement: about $0.0136 per narration minute
and about $0.0115 per cold one-job visit [`testpack/notes/cost_modal.md` section 8].

## 6. Honest limits

- All relevance numbers are from one rater (me). The reviewers' scores will decide the Core and Outstanding rows;
  my fresh-script 2.67 shows the system is not yet robust on new input.
- Samples are small (80, 66 and 9 rated scenes); run 3 was tuned on scenes I had already rated.
- Tamil is still below English; one Tamil scene failed because Whisper misheard the speech itself.
- The judge is a Groq *preview* model with a small free quota, so it rates only a few uncertain scenes per job.
- Story moments ("a storm rolled in") and lines naming specific items stay hard for stock footage.
- A 60 s job takes about 5.5 minutes on this PC; on Modal's 2 cores it is probably slower (only an 8.75 s job was
  measured there: 59.99 s).
- Docker Compose and the MinIO test were not re-run in the last sessions; clip swap was not re-timed in the test pack
  (earlier real run: full render 108.9 s vs swap 23.5 s); the sailboat crop fix is tested but was not re-rendered.

## 7. Next steps

1. Implement the planned fix above (action-sized scenes, action-first queries) and re-test on new unseen scripts.
2. Get the reviewers' scores; re-run the judge-vs-human comparison on far more than 7 scenes.
3. Time a 60 s narration on Modal (only the 8.75 s check exists) and replace the cost estimate with measured Modal times.
4. Reduce turnaround (footage and render are about 80% of it) before raising the Modal job limits.
5. More Tamil / Hindi voices to check "as well as English".
