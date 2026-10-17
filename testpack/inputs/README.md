# Test pack inputs

23 inputs: 13 "core" (varied real-world content) + 10 hard cases. Every input is rights-clean: my own voice
recordings, our own Kokoro TTS voices (Apache-2.0) reading texts I wrote for this test pack, or audio made by
FFmpeg. No copyrighted media, no third-party recordings. All texts are original; the news items are invented
samples, not real events. **TTS** = the voice is synthetic (Kokoro), not a person.

- Run them all: `.venv\Scripts\python tools\batch_run.py testpack\inputs --out testpack\outputs\run1 --ratios 16:9,9:16 --keep-input-ratios`
  (a `.json` input that names its own `aspect_ratios` keeps them: three inputs also make 1:1).
- The generated audio files are rebuilt with `.venv\Scripts\python tools\make_test_inputs.py` (sources in `tts_sources/`).
- Expected outcome of each input (written before the run): `testpack/expected.json`.
- Length = narration length measured in run1 (`testpack/outputs/run1/summary.csv`), or the file length for inputs
  that make no video.
- `my_voice/` holds my original recordings (`*.m4a.m4a`, as recorded); `core01`-`core03` are byte-for-byte copies
  with clear names. They are my own voice: personal data I chose to share for this assessment.

## Core inputs

| File | Type | Purpose | Voice / source | Language | Length | Rights |
|---|---|---|---|---|---|---|
| core01_en_saving_tips_myvoice.m4a | audio upload | tutorial-style money tips | **my own voice** (Indian English accent) | en | 24.9 s | my recording |
| core02_en_bakery_story_myvoice.m4a | audio upload | short story / promo | **my own voice** | en | 19.9 s | my recording |
| core03_ta_screen_time_myvoice.m4a | audio upload | explainer on screen time, with a year + "four hours" | **my own voice** | ta (Tamil) | 28.3 s | my recording |
| core04_tutorial_cold_brew.json | script (TTS) | tutorial; 16:9 + 9:16 + **1:1** | TTS Kokoro `af_heart` (en-US-female) | en | 36.4 s | own text + Kokoro |
| core05_news_summary.json | script (TTS) | news summary (invented city news) | TTS Kokoro `bm_george` (en-GB-male) | en | 36.5 s | own text + Kokoro |
| core06_product_promo.json | script (TTS) | product promo (unbranded bottle); **1:1** too | TTS Kokoro `am_michael` (en-US-male) | en | 25.5 s | own text + Kokoro |
| core07_story_lighthouse.json | script (TTS) | story | TTS Kokoro `bf_emma` (en-GB-female) | en | 46.6 s | own text + Kokoro |
| core08_motivational_talk.json | script (TTS) | motivational talk | TTS Kokoro `hm_omega` reading English (en-IN-male: Hindi speaker voice) | en | 37.2 s | own text + Kokoro |
| core09_explainer_solar.json | script (TTS) | longer explainer (how solar panels work) | TTS Kokoro `hf_alpha` reading English (en-IN-female) | en | 87.4 s | own text + Kokoro |
| core10_scene_list_incubrix.json | IncuBrix scene list `[{text, visual_hint}]` | scene list with visual hints; **1:1** too | TTS Kokoro `af_heart` | en | 23.9 s | own text + Kokoro |
| core11_hi_tts_saving.m4a | audio upload (made by TTS) | Hindi voiceover: saving money, "ten percent" in words | **TTS** Kokoro `hf_alpha` (hi-IN-female), text `tts_sources/core11_hi_tts_saving.txt` | hi (Hindi) | 18.5 s | own text + Kokoro |
| core12_hi_tts_monsoon.m4a | audio upload (made by TTS) | Hindi voiceover: monsoon / saving water | **TTS** Kokoro `hm_omega` (hi-IN-male), text `tts_sources/core12_hi_tts_monsoon.txt` | hi | 22.7 s | own text + Kokoro |
| core13_hi_script_tea.json | script (TTS) | Hindi script through the API's own TTS (language hint from the voice) | **TTS** Kokoro `hm_omega` (hi-IN-male) | hi | 21.9 s | own text + Kokoro |

I cannot record Hindi myself, so all three Hindi inputs are TTS and labelled as such.

## Hard cases

| File | Hard case | Source | Length | Expected |
|---|---|---|---|---|
| hard01_abstract_and_statistics.json | abstract lines ("Trust is earned slowly") + statistics ("Our sales grew 40% in 2025", "three out of four buyers") | TTS `af_heart` | 19.3 s | video; number cards for statistic lines |
| hard02_named_person_and_brand.json | real named people (Satya Nadella, Sachin Tendulkar) + brands (Microsoft, Apple, Coca-Cola); only factual statements | TTS `bm_george` | 19.5 s | video, generic footage, no faces/logos, warnings |
| hard03_double_meanings.json | bank (river / money), cloud (sky / computing), mouse (animal / computer), python (snake / language), value | TTS `am_michael` | 30.2 s | video; the right sense per line |
| hard04_sensitive_news.json | sad / sensitive news (floods, deaths; invented, no real event) | TTS `bf_emma` | 26.2 s | video; calm, respectful footage, no graphic content |
| hard05_crop_subject_at_edge_9x16.json | 9:16 with subjects at the edge of the frame (scene list with "left / right edge" visual hints) | TTS `af_heart` | 15.6 s | video; crop decision per scene recorded (see `testpack/outputs/run1/hard05_crop_subject_at_edge_9x16/crop_check/`) |
| hard06_five_minute_voiceover.m4a | about 5-minute voiceover (talk on keeping cities cool) | **TTS** Kokoro `am_michael`, text `tts_sources/hard06_five_minute_voiceover.txt` | 293.5 s | video |
| hard07_silent_audio.wav | silent audio | FFmpeg `anullsrc` (digital silence) | 20.0 s | `NO_SPEECH_DETECTED` |
| hard08_music_only_chords.wav | music-only audio (no voice) | FFmpeg `aevalsrc`: synthesised C / Am / F / G chords with a soft pulse | 30.0 s | `MUSIC_ONLY_AUDIO` |
| hard09_wrong_type_text_renamed.mp3 | wrong file type: a plain .txt file renamed to .mp3 | written by hand | 90 bytes | `UNSUPPORTED_FILE_TYPE` |
| hard10_empty_script.txt | empty script | empty file (0 bytes) -> `{"script": ""}` | - | `EMPTY_SCRIPT` |

Tamil / Hindi rule tested by core03, core11-13: the footage search queries are English (the meaning is
translated: Whisper's translate pass + Groq), while captions stay in Tamil / Hindi (Noto fonts, SIL OFL).

Other hard cases (provider down, corrupted download, two users at once, Groq outage) need a broken environment,
not a special input: see `testpack/resilience/README.md`.
