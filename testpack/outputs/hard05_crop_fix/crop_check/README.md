# Hard case 5 (9:16 subject at the edge): crop fix, 2026-10-19

**Cause (live run `testpack/live/hardcases_2026-10-19/hard05_*`):** the CLIP crop only tried 3-4 FIXED windows
(left / centre / right / salient) and broke near-ties toward salient / centre. Its "keeps the subject whole" test only
checked that no window EDGE fell inside the subject, so a window that left the subject out entirely counted as "whole":
scene 2 (sailboat at x = 0.60-0.72) got the empty left third; scene 3 (cyclist's face at about x = 0.12-0.47) got the
centre window, which cut the face in half.

**Fix (`app/pipeline/render.py`, `_clip_crop`, `locate_subject`, `place_window`):** 13 narrow strips across each sampled
frame are scored with CLIP against the scene meaning; the strongest run of strips is the subject (on the frames' average,
or in every frame for a moving subject). The 9:16 window is placed anywhere so that it holds the WHOLE subject; it pans
smoothly if the subject moves at least 15% of the frame one way; no clear subject -> centre crop + a job warning.

`live_clips_before_after_9x16.jpg` (the same live clips, re-rendered locally with the new code), left to right:
scene 2 before (empty haze) | scene 2 after (whole sailboat) | scene 3 before (face cut) | scene 3 after, first and last frame.

`local_rerun_scenes2_3.jpg`: the full local re-run of hard05 (16:9 | 9:16 for scenes 2 and 3). Footage selection picked a
different clip for scene 2 locally (a notebook close-up, no single subject -> centre crop + warning); scene 3 is the same
cyclist clip, whole face in the 9:16 frame. Not yet deployed to Modal.
