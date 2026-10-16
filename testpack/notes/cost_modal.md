# Modal cost estimate for the live demo

Status: **estimate**. Prices are Modal's published prices; container times on Modal are **not measured yet**
(the app has not been deployed when this was written). Replace the assumed times below with real ones from
`tools/live_check.py --cold` and the `modal container stopped ... uptime_seconds` log line.

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
- `min_containers=0`, `max_containers=1`, `scaledown_window=600` s.

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

## 4. Per job and per idle period

Assumptions (NOT measured on Modal): cold start **C = 60 s**, one short-script job **T = 120 s**
(my PC: 94 s above; an earlier real Groq + Pixabay job took 114 s; Modal's CPUs may be slower or faster).

| Item | Seconds | Cost |
|---|---|---|
| One job inside a container that is already up (T) | 120 | 120 x 0.00003286 = **$0.0039** |
| Idle tail after the last request (scaledown_window) | 600 | 600 x 0.00003286 = **$0.0197** |
| Idle per hour while a container is up | 3,600 | **$0.1183** |
| One cold visit with one job (C + T + idle tail) | 780 | 780 x 0.00003286 = **$0.0256** |
| Zero traffic (no container) | - | **$0** |

So the idle tail, not the job, is most of the cost of a visit. `MODAL_SCALEDOWN_SECONDS=300` would make a
one-job visit 480 s = $0.0158, but a job whose client stops polling for 5 minutes could then be cut off
(it is re-queued once on the next start, see `recover_interrupted_jobs`).

## 5. What $1 / month allows

- About **39 cold one-job visits** (30,432 / 780), or about one visit a day for a month ($0.77).
- **Warning about `DEMO_MAX_JOBS_PER_DAY=40` (the requested default):** 40 jobs in 40 separate visits =
  40 x $0.0256 = **$1.02 in one day** = the whole month's credit. Even 40 jobs back-to-back in one warm container
  = (60 + 40 x 120 + 600) s = 5,460 s = $0.18 per day. For a review period on $1, deploy with a lower cap, e.g.
  `$env:DEMO_MAX_JOBS_PER_DAY = "5"` (worst case 5 x $0.0256 = $0.13 per day).
- A 5-minute voiceover (the demo maximum) takes longer than T above; not measured.
- Volume storage: outputs of a few hundred jobs are a few GiB, inside the free 1 TiB.

## 6. Not counted / unknown

- Whether the time to start a container (before `/ready`) is billed exactly like running time: assumed yes.
- Image builds (each `modal deploy` after a code change re-runs the offline model check): not stated on the
  pricing page.
- Groq and Pixabay calls are free-tier APIs outside Modal.
