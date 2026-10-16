# Logo check calibration (2026-10-16)

**Why:** in the last real run, the old logo rule (logo score − scene score ≥ −0.04) rejected 19–24 Pixabay
video candidates per scene. The final video used no stock video at all, only photos and cards.

**What the check is now** (app/pipeline/rank.py, settings in app/config.py):
- The check gives every picture a *logo probability* from 0 to 1, using CLIP (ViT-B-32, laion2b_s34b_b79k).
- It is a softmax over 5 logo prompts plus 10 neutral prompts, with the logo prompts' shares added up.
- Each neutral prompt is the same kind of subject *without* the logo ("a photo of a laptop" vs. "a photo
  of a laptop with an apple logo").
- The highest value over the whole picture and a 3×3 grid of crops counts (`LOGO_CROP_GRID=3`).
- **Rejected** if the probability is above `LOGO_REJECT_PROB=0.6`.
- **Penalised** (order score −0.03) if it is above `LOGO_PENALTY_PROB=0.4`.
- Only candidates that already pass `STOCK_MIN_SCORE` are checked. The same test runs on 3 frames of the
  downloaded clip.

Repeat the measurement with:
`python tools/logo_calibration.py score --labels testpack/calibration/logo_labels.csv`
(images: `python tools/logo_calibration.py fetch` → `data/calibration/`, git-ignored; no images are in git).

## Data and labels

| Set | File | Pictures | Logo | No logo | Used for |
|---|---|---|---|---|---|
| Calibration | `logo_labels.csv` | 82 | 15 (9 clear, 6 small) | 67 | choosing prompts, crop grid, thresholds |
| Held-out | `logo_labels_holdout.csv` | 40 | 6 (3 clear, 3 small) | 34 | checking only; labelled **before** it was scored |

- **The pictures:** real Pixabay video thumbnails, the same small picture the pipeline scores.
- **Calibration queries:** 58 from laptop, phone, office, city street, food, nature, money, handshake,
  server room and shopping, plus 24 from logo-prone queries (times square, soda can, sneakers, macbook, car,
  takeaway coffee). Without these the set had too few logos to measure recall.
- **Held-out queries:** coffee shop, smartphone, supermarket, street at night, computer, football, drink
  bottle, airport, typing keyboard, store.
- **Labelling rule** (I looked at every picture; borderline ones I zoomed into): `logo` = a brand mark or a
  brand / company name readable at thumbnail size. `visibility` = `clear` or `small` (small sign,
  embossed mark, small laptop logo). Banknote text, book covers and unreadable signs = `no_logo`.
  Shop signs of local businesses were counted as `logo`. That is a judgement call; another person might
  label 5–6 pictures differently.
- Every picture's probability is in `logo_scores.csv` / `logo_scores_holdout.csv`.

## Results at the chosen thresholds

"Precision" = of the rejected pictures, how many really show a logo. "Recall" = of the logo pictures, how
many were rejected. "Clean kept" = clean pictures that are NOT rejected (the number that matters most here).

| Rule | Set | Rejected | Precision | Recall | Clean kept |
|---|---|---|---|---|---|
| **New rule (> 0.6)** | calibration (82) | 9 | **89%** (8/9) | **53%** (8/15) | **99%** (66/67) |
| **New rule (> 0.6)** | held-out (40) | 3 | **67%** (2/3) | **33%** (2/6) | **97%** (33/34) |
| Prompts from the brief, whole picture (> 0.6) | calibration | 20 | 10% | 13% | 73% |
| Prompts from the brief, whole picture (> 0.6) | held-out | 10 | 40% | 67% | 82% |
| Old margin rule (−0.04) | calibration | 18 | 6% | 7% | 75% |
| Old margin rule (−0.04) | held-out | 6 | 17% | 17% | 85% |

(The old rule is approximated with "a photo of <query>" as the scene text.)

**How the thresholds were picked:**
- On the calibration set, every threshold from 0.6 to 0.7 gives exactly the same result, so I kept the
  suggested start value, 0.6.
- On the held-out set, 0.7 would remove its one false rejection. Choosing it there would be tuning to the
  test set, so I did not.
- Penalty band (0.4–0.6): 3 calibration pictures fall in it, and all 3 are clean. The penalty only
  changes the order of candidates, never whether a clip is used.

## What did not work (tried in this order, calibration set)

| Try | AUC* | At 0.6 |
|---|---|---|
| Prompts from the brief ("a visible brand logo" ... vs. "a stock video frame", "an everyday scene with no logos", ...) | 0.33 (worse than chance) | 2/15 logos, 18/67 clean rejected |
| Same logo prompts, matched neutral prompts | 0.40 | 0/15 logos |
| Matched pairs, whole picture only | 0.72 | 2/15 logos, 0 clean rejected |
| Matched pairs + whole picture + 4 quarters + centre | 0.86 | 5/15 logos, 0 clean rejected |
| **Matched pairs + whole picture + 3×3 grid (chosen)** | 0.85 | 8/15 logos, 1 clean rejected |

\*AUC = chance that a random logo picture scores higher than a random clean one (0.5 = coin flip).

Why the brief's prompts fail:
- **"No logos" backfires.** CLIP ignores the "no", so "an everyday scene with **no logos**" matched
  logo pictures best of all.
- **"Device" and "watermark" fire on clean shots.** "A company logo on a device" fired on every laptop and
  phone, and "a watermark" on soft, blurry shots.
- **Logos are small in the frame.** Scored on the whole picture, a laptop logo or shop sign is only a small
  part of it. The crops fix part of that.

## Where it still fails (honest list)

- **Missed clear logos** (calibration): TOPSHOP shop signs (1044), PANDORA / EAT. signs (1887), an Apple logo
  on a laptop lid outdoors (202134). Missed on held-out: Allied Aviation truck + United tails (3586).
  Recall is about half on the calibration set and a third on the held-out set: **this check does not
  guarantee a logo-free video.**
- **False rejections:**
  - a blurred shopping crowd (3626, calibration);
  - a laptop showing a web page (39892, held-out).
  - In the real run below, also a clean laptop + notebook + cup (368623).
- **Small sets:** only 21 logo pictures in total, so every recall number has a wide error margin
  (1 picture = 5–17 points).
- **Not tested here:** text watermarks. Pixabay API downloads have none, so there were no examples.

## Real run after the change ("Saving money..." script, 16:9 + 9:16)

| | Before (job 56bfb185, old rule) | After (job d9eb8378, new rule) |
|---|---|---|
| Video candidates rejected by the logo check per stock scene | 19, 24, 23 | 1, 3, 0 |
| Scenes using stock video / photo / card | 0 / 2 / 2 | **2 / 0 / 2** |
| Share of non-statistic scenes with real video | 0% | **67%** (2 of 3) |

- **The rejections, checked by eye:**
  - 273921: a real Apple logo, correct.
  - 34485: a readable "bcn2012.org" poster, borderline.
  - 368623 and 39892: no logo, wrong.
- **The two chosen clips:** no logo visible in their thumbnails or in the frames I checked.
- **The remaining card scene** ("Trust is earned slowly...") was not caused by the logo check: no source
  reached `STOCK_MIN_SCORE` 0.22 (best: video 0.196, photo 0.211, Wikimedia 0.164).
