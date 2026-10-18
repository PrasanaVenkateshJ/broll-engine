# Modal cost estimate for the live demo

Status: **estimate**. Prices are Modal's published prices; container times on Modal are **not measured yet**
(the app has not been deployed when this was written). Replace the assumed times below with real ones from
`tools/live_check.py --cold` and the `modal container stopped ... uptime_seconds` log line.

**Current request (since 2026-10-18): 2 cores, 5 GiB request / 8 GiB limit, `RENDER_WORKERS=1`; see section 8.** Sections 2-7 keep
the earlier 3 GiB numbers for the record.

## 1. Prices (modal.com/pricing, read 2026-10-16)

| Resource | Published price | Note |
|---|---|---|
| CPU | $0.0000131 per physical core per second | 1 core = 2 vCPU; minimum 0.125 cores |
| Memory | $0.00000222 per GiB per second | minimum 128 MiB |
| Volume storage | $0.09 per GiB per month | first 1 TiB per month free |
| Billing rule | "whichever is higher: the amount you request or the amount you actually use" | per second while a container runs |

The pricing page lists $30/month of free credit for the Starter plan; this account has about **$1/month**,
so the estimate below uses $1. The page does not say whether image builds are billed.

## 2. What the demo requests (deploy/modal/modal_app.py)

- `cpu=2` (cores), `memory=(3072, 6144)` = 3 GiB request, 6 GiB hard limit.
- `min_containers=0`, `max_containers=1`, `scaledown_window=180` s (was 600 s; lowered so the $1 lasts a month).
- Cost guards: `DEMO_MAX_JOBS_PER_DAY=12`, `DEMO_MAX_JOBS_PER_MONTH=35` (new jobs + clip swaps, all tokens).

**Why 3 GiB and not 6 GiB** (measured, not on Modal): one real job on my Windows PC (Kokoro + Whisper small +
CLIP ViT-B-32, rules understanding, Wikimedia + generated cards, 37-word script, 16:9 + 9:16 + 1:1, `WORKER_CONCURRENCY=1`,
`FOOTAGE_WORKERS=2`), memory sampled every 0.25 s with psutil:

| Moment | Memory (resident) |
|---|---|
| Python process after imports | 81 MiB |
| After CLIP preload (= idle, before the first job) | 1,007 MiB |
| Peak during the job, Python process | 2,507 MiB |
| Peak during the job, Python + ffmpeg child processes | **2,942 MiB** |
| After the job (all models stay loaded) | 2,182 MiB |
| Job time on my PC | 94.1 s |

Limits of this measurement: Windows, not Linux; sampling can miss very short spikes; no Groq / Pixabay video
(real Pixabay clips can make ffmpeg use more); not a 5-minute voiceover. Because Modal bills
max(request, use), a 3 GiB request costs the same as 6 GiB whenever the job really needs more, but half as much
while the container is idle. The 6 GiB hard limit only stops a runaway job.

## 3. Price per second of one running container

```
CPU     2 cores  x $0.0000131    = $0.0000262  / s
Memory  3 GiB    x $0.00000222   = $0.00000666 / s
Total                            = $0.00003286 / s   = $0.1183 / hour
```

If real memory use goes above 3 GiB, that part is billed at the real use (worst case at the 6 GiB limit:
$0.0000395 / s = $0.142 / hour).

$1 buys `1 / 0.00003286 = 30,432 s` = **about 8.5 hours of container time per month.**

## 4. Per job and per idle period (estimate)

Assumptions (NOT measured on Modal): cold start **C = 60 s**, one short-script job **T = 120 s**
(my PC: 94 s above; an earlier real Groq + Pixabay job took 114 s; Modal's CPUs may be slower or faster).

| Item | Seconds | Cost |
|---|---|---|
| One job inside a container that is already up (T) | 120 | 120 x 0.00003286 = **$0.0039** |
| Idle tail after the last request (scaledown_window) | 180 | 180 x 0.00003286 = **$0.0059** |
| Idle per hour while a container is up | 3,600 | **$0.1183** |
| **One cold visit with one job (C + T + idle tail)** | 360 | 360 x 0.00003286 = **$0.0118** |
| One cold visit with NO job (open the Studio, `/ready`) (C + idle tail) | 240 | 240 x 0.00003286 = **$0.0079** |
| Zero traffic (no container) | - | **$0** |

Before this change (600 s tail) a one-job visit was 780 s = $0.0256, so a visit now costs about 46% of that.

**Why 180 s does not cut jobs off:** the tail only starts after the LAST request. While a job runs, its client
polls it (the Studio every 1-5 s, 10 s in a hidden tab; `tools/batch_run.py` and `tools/live_check.py` up to
every 15 s), and each poll keeps the container up. Only a client that stops polling for more than 180 s mid-job
(closed tab, lost network) lets the container stop; that job is then queued once more when the next container
starts, and fails with `JOB_INTERRUPTED` (retryable) if it is cut off a second time
(`recover_interrupted_jobs`, tested in `tests/unit/test_cost_guards.py`).

## 5. What $1 / month allows (estimate)

- $1 = 30,432 s = about **84 cold one-job visits** (30,432 / 360).
- **Monthly cap 35 jobs:** worst case = 35 jobs in 35 separate cold visits = 35 x $0.0118 = **$0.41**. That leaves
  about $0.59 = about **74 visits without a job** (Studio opened, `/ready` checked), which no cap counts.
- **Daily cap 12 jobs:** worst case 12 separate visits = **$0.14 in one day**; 12 jobs back-to-back in one warm
  container = (60 + 12 x 120 + 180) s = 1,680 s = $0.055. The monthly cap still limits the month to 35.
- The caps count per UTC calendar month. If the review period crosses the 1st of a month, the job cap resets
  then; this assumes the Modal credit also renews monthly (not checked).
- A 5-minute voiceover (the demo maximum) takes longer than T above; not measured. Ten such jobs at an assumed
  600 s each would add about 10 x 480 s x 0.00003286 = $0.16 compared with the table.
- Volume storage: outputs of a few hundred jobs are a few GiB, inside the free 1 TiB.

## 6. Not counted / unknown

- Whether the time to start a container (before `/ready`) is billed exactly like running time: assumed yes.
- Image builds (each `modal deploy` after a code change re-runs the offline model check): not stated on the
  pricing page.
- Groq and Pixabay calls are free-tier APIs outside Modal.

## 7. Update 2026-10-17 (measured locally during the test pack, not on Modal)

- One job at a time (run1, 19 real jobs incl. the 5-minute voiceover): worker exact peak **4,109 MB**; worker memory
  was above 3.5 GB in 591 one-second samples spread over several ordinary jobs, not only the long one
  (`testpack/logs/memory_perf_and_run1_part2.csv`, `testpack/logs/peaks_after_run1.txt`).
- So the 3 GiB request above is often exceeded; Modal then bills the real use (up to the 6 GiB limit, which was
  never reached). A fairer per-second price while a job runs is closer to the 4 GiB figure:
  2 x 0.0000131 + 4 x 0.00000222 = $0.0000351 / s.
- Measured processing time on this PC: median 365 s per narration minute (run1, 16:9 + 9:16), see `testpack/metrics.md`.
  Modal times are still NOT measured.

## 8. Update 2026-10-18: memory sized for one render worker, redeployed

**Measured locally (this PC, Windows; NOT on Modal), one job at a time, one render worker** (run 1, 19 real jobs incl. the
5-minute voiceover; `testpack/logs/memory_perf_and_run1_part2.csv`, `peaks_after_run1.txt`): worker exact peak
**4,109 MB**; worker + ffmpeg together at most **4,396 MB** in 1-second samples (ffmpeg up to 876 MB).
With 3 parallel render workers (this PC's default since Session 7B, not used on Modal) the total reached 5,204 MB
(`testpack/logs/memory_run3.csv`, ffmpeg alone 2,250 MB), so the extra render workers cost memory, not much else.

**Setting:** `MODAL_MEMORY_MB=5120` request (4,396 MB + ~15%), `MODAL_MEMORY_LIMIT_MB=8192`, `RENDER_WORKERS=1`,
`QUALITY_MODE=balanced`. Limits of the measurement: the sampler can miss short spikes; Linux on Modal may differ;
the Groq vision judge and Openverse add little memory but were not sampled alone.

```
CPU     2 cores  x $0.0000131    = $0.0000262  / s
Memory  5 GiB    x $0.00000222   = $0.0000111  / s
Total                            = $0.0000373  / s   = $0.1343 / hour      ($1 = 26,810 s = 7.4 hours)
```

Estimate per cold one-job visit = cold start + job + idle tail. Cold start **measured on Modal 2026-10-18: `/ready` answered
after 6.9 s** (`testpack/live/live_check_2026-10-18_cold.txt`; the models are baked into the image). The job time on Modal is
**not measured with this code** (the last real Modal job, 2026-10-16 with the old code, was 44 s for 8.75 s of narration);
assuming 120 s: (7 + 120 + 180) s x 0.0000373 = **$0.0115**; 35 such visits (the monthly cap) = **$0.40** of the ~$1 credit.
Cost per narration minute at the median 365 s per minute measured on this PC: 365 x 0.0000373 = **$0.0136** (estimate; a
2-core Modal container may be slower than this 16-thread PC).
