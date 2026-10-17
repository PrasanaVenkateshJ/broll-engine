# Crop decisions: hard05_crop_subject_at_edge_9x16 (run1, 2026-10-17)

Frames: the middle frame of each scene, 16:9 (left) next to 9:16 (right): `scene_N_16x9_vs_9x16.jpg`,
all four stacked in `all_scenes.jpg`. Numbers: `segments.<ratio>.crop` in `../timeline.json`.

The visual hints asked Pixabay for subjects at the edge of the frame. Pixabay did not always return such clips,
so only some scenes really test the crop. Read honestly:

| Scene | Clip | Source shape | Decision | What the frames show | Verdict |
|---|---|---|---|---|---|
| 0 "lone tree on the left side..." | pixabay_video 323715 | 16:9 | 9:16: window `centre` (0.259); `left` 0.267, `right` 0.259, whole frame 0.235 (left only 0.008 higher: near-tie -> centre) | rice fields, no lone tree in the clip | no edge subject to keep: not a real test |
| 1 "lighthouse at the right edge..." | pixabay_video 216997 | 16:9 | 9:16: `centre` (0.312 = best window) | lighthouse sits just right of centre; the 9:16 crop keeps all of it | correct |
| 2 "small sailboat far on the left..." | pixabay_video 360429 | **portrait 1440x2560** | 9:16: `fit` (same shape). 16:9: vertical crop, window `salient` y=0.53-0.85 (0.304) beat `centre` y=0.34-0.66 (0.302) by 0.002; near-ties go to salient first | 9:16 shows the boat fine. In **16:9 the boat is cut at the top edge** of the picture; the centre window would have kept it whole | **weakness**: the near-tie rule preferred the salient window that slices the subject |
| 3 "cyclist riding along the right edge..." | pixabay_video 10886 | 16:9 | 9:16: `centre` (0.257); left 0.261, right 0.254 (all within 0.008, a near-tie) | blurred street with road markings; a blurred figure at the right edge is dropped by the centre crop | CLIP saw no subject (right scored lowest); dropping the blurred figure is acceptable |

Take-away: the crop never froze or failed and always recorded its reason, but with near-equal window scores
the "salient first" tie rule can cut a small subject at the window edge (scene 2, 16:9). A possible fix
(not made in this step): on a near-tie, prefer the window whose CLIP score per frame is most stable, or check
that the salient window does not start/end inside the high-edge-energy region.
