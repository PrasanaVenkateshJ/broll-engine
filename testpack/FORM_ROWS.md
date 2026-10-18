# Submission Form rows (copied from the IncuBrix Excel form; use these exact numbers/wording in testpack/INDEX.md)


## Must-haves (sheet 2_Must_haves, column "Met?")

- **1**: Both inputs work: audio (mp3, wav, m4a, up to 10 minutes) and text (you generate the narration).
- **2**: Scenes by meaning, timed to the voice. Scene changes fall on sentence or phrase boundaries; every second of narration is covered; the narration sets the timing, not the clips.
- **3**: Every clip is licence-clean. Source, licence, attribution and link are recorded per clip; all allow commercial use; no watermarked footage; AI-generated clips are flagged.
- **4**: Correct, consistent video. All requested aspect ratios; every clip normalised (resolution, frame rate, pixel format); no black frames, frozen frames or audio drift.
- **5**: No repeats and no nonsense matches. No clip is used twice in one video; footage matches the meaning, not just a keyword.
- **6**: Safe output. No AI-generated realistic faces of real people; no invented brand logos; respectful footage for sensitive topics.
- **7**: Resilient jobs. Jobs run in the background with progress; if one footage source fails, the job still finishes using another source or a fallback.
- **8**: Integration Standard followed and the live demo works for the whole review period.

## Levels (sheet 3_Levels)

- **Core**: All must-haves pass.
- **Core**: 10 varied inputs (for example a tutorial, a news summary, a product promo, a story; different accents; 20 seconds to 5 minutes) all produce usable videos.
- **Core**: Average relevance of at least 3.5 out of 5 from our reviewers on your 10 test videos.
- **Advanced**: At least two footage sources (for example a stock library and a generative video or image-to-video model) with a written, measurable rule for choosing between them, and the reason for each choice stored in timeline.json.
- **Advanced**: Swap a clip: the user replaces one scene's clip and only the affected part is re-rendered, in clearly less time than a full render.
- **Advanced**: Looks like one video: colour and brightness normalised across sources; sensible pacing (no flashing 0.5-second clips).
- **Advanced**: Accepts IncuBrix scene lists (text plus visual hint per scene).
- **Outstanding**: Measured relevance: an independent vision-language model scores each clip against its narration, and you show how well that score agrees with human ratings.
- **Outstanding**: Multilingual narration: Hindi and Tamil voiceovers work as well as English.
- **Outstanding**: Production numbers: time and cost to produce one minute of finished video, and how many jobs one server handles at once.
- **Outstanding**: At least 85% of scenes rated 4 or 5 out of 5 by our reviewers.

## Integration Standard (sheet 4_Integration)

- **1. One-command start and test**: docker compose up starts the service and everything it needs (database, storage, local test servers). One command runs all tests (unit, integration and at least one end-to-end) with no API keys. A .env.example lists every setting.
- **2. Documented HTTP API**: An openapi.yaml (OpenAPI 3.1) that matches the running service exactly. All routes under /api/v1/.
- **3. Background jobs**: Anything slower than 2 seconds runs as a job: POST returns 202 with a job_id; GET /api/v1/jobs/{job_id} returns status (queued, running, succeeded, failed), progress 0-100 and result links. Optional callback URL, signed with HMAC-SHA256 in an X-IncuBrix-Signature header.
- **4. Safe retries**: An Idempotency-Key header on every POST: sending the same request twice with the same key returns the same job and never does the work twice.
- **5. Clear errors**: Errors are JSON: {"error": {"code", "message", "retryable", "details"}}. Codes are UPPER_SNAKE_CASE and listed in the README.
- **6. Configuration**: All settings, paths, model names and limits come from environment variables or a config file. No hard-coded paths or keys.
- **7. Files in, files out**: Inputs accepted as an upload or a URL. Outputs saved to S3-compatible storage (MinIO in the compose file is fine) and returned as download links.
- **8. Logs and health**: Structured JSON logs with timestamp, level, job_id and the X-Correlation-Id from the request. GET /health and GET /ready endpoints.
- **9. Output manifest**: Every result includes a JSON manifest: which models/providers and versions were used, settings, seed, timings, cost estimate, warnings, and licence/rights information.
- **10. Batch runner**: A command-line tool that runs a folder or file of test inputs against the API and writes all results to one folder. We use this to test every submission the same way.

## Metrics (sheet 5_Metrics)

- **Turnaround**: Time from upload to finished video for 60 s of narration, cold and warm.
- **Cost**: Cost per minute of finished video (footage APIs, generation, TTS, compute).
- **Coverage**: Share of narration time covered by a clip you rate 3 or more out of 5.
- **Relevance**: Your own 1-5 rating per scene (and the independent model score for Outstanding).
- **Reliability**: Render success rate across all tests; repeat-clip rate (must be 0).
- **Resources**: Peak memory and GPU time, if you used a GPU.

## Hard cases (sheet 6_Hard_cases)

- **1**: Abstract lines ("trust is earned slowly") and numbers or statistics ("sales grew 40% in 2025").
- **2**: A real, named person or brand mentioned in the narration.
- **3**: Words with double meanings: value, bank, cloud, mouse, python.
- **4**: A sad or sensitive news story.
- **5**: A 9:16 output where the important subject sits at the edge of a 16:9 clip.
- **6**: A 5-minute voiceover; silent audio; music-only audio; a wrong file type; an empty script.
- **7**: A footage provider that is down or returns nothing; a corrupted download; two users submitting at the same time.
