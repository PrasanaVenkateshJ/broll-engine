"""All settings come from environment variables (or a local .env file).

Nothing else in the app should read os.environ directly: it asks get_settings().
Every field here is also listed, with a comment, in .env.example.
"""
from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Read env vars; also read .env if it exists. Unknown env vars are ignored.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- General ---
    APP_NAME: str = "broll-engine"
    APP_VERSION: str = "0.2.0"
    LOG_LEVEL: str = "INFO"
    # Base URL used in links the worker sends (callbacks). The API uses the
    # request's own URL for links unless this is set explicitly.
    PUBLIC_BASE_URL: str = ""

    # --- Auth + rate limit ---
    # Comma-separated list of allowed bearer tokens. Empty = auth switched OFF.
    API_TOKENS: str = ""
    # Requests per token per minute, counted separately (0 = no limit):
    # POST = job creation + clip swaps (each one starts slow work); GET = job status + file downloads
    # (cheap, and clients poll while they wait).
    RATE_LIMIT_POST_PER_MINUTE: int = 30
    RATE_LIMIT_GET_PER_MINUTE: int = 600
    # Most jobs GET /api/v1/jobs may return in one answer (?limit=1..this).
    JOBS_LIST_MAX: int = 100
    # Cost guard for a paid-per-second live demo: at most this many NEW jobs (incl. clip swaps) per UTC day,
    # for all tokens together. Over it -> 429 DAILY_LIMIT_REACHED until midnight UTC. 0 = no cap.
    DEMO_MAX_JOBS_PER_DAY: int = 0
    # Same, per UTC calendar month. Over it -> 429 MONTHLY_LIMIT_REACHED until the 1st of next month. 0 = no cap.
    DEMO_MAX_JOBS_PER_MONTH: int = 0

    # --- Studio (the web UI at GET /, files under /studio/) ---
    STUDIO_ENABLED: bool = True
    # Folder with index.html, studio.css and the .js files. Empty = app/studio next to the code.
    STUDIO_DIR: str = ""
    # Sites allowed to show the Studio inside a frame (CSP frame-ancestors), space-separated, e.g.
    # "https://huggingface.co" for a Hugging Face Space page. Empty = no site may frame it.
    STUDIO_FRAME_ANCESTORS: str = ""

    # --- Database ---
    DATABASE_URL: str = "sqlite:///./data/broll.db"
    # SQLite journal: WAL lets readers and a writer work at the same time (it needs shared memory, so a
    # normal local disk). DELETE = classic journal file: the cautious choice on a network-style disk such
    # as a Modal Volume (Modal does not document WAL support there).
    SQLITE_JOURNAL_MODE: Literal["WAL", "DELETE"] = "WAL"

    # --- Storage ---
    STORAGE_BACKEND: Literal["local", "s3"] = "local"
    LOCAL_STORAGE_DIR: str = "./data/storage"
    S3_ENDPOINT_URL: str = "http://localhost:9000"
    S3_ACCESS_KEY: str = "minioadmin"
    S3_SECRET_KEY: str = "minioadmin"
    S3_BUCKET: str = "broll"
    S3_REGION: str = "us-east-1"

    # --- Signed download links ---
    FILE_LINK_SECRET: str = "change-me-file-link-secret"
    FILE_LINK_TTL_SECONDS: int = 3600

    # --- Uploads ---
    MAX_UPLOAD_MB: int = 100
    ALLOWED_AUDIO_EXTENSIONS: str = "mp3,wav,m4a"
    WORK_DIR: str = "./data/work"

    # --- Callbacks ---
    CALLBACK_SECRET: str = "change-me-callback-secret"
    CALLBACK_MAX_RETRIES: int = 3
    CALLBACK_BACKOFF_SECONDS: float = 1.0
    CALLBACK_TIMEOUT_SECONDS: float = 10.0

    # --- Worker ---
    WORKER_CONCURRENCY: int = 2
    WORKER_POLL_SECONDS: float = 1.0
    # Keep each job's temp folder (WORK_DIR/<job_id>) after the job, for debugging.
    KEEP_WORK_FILES: bool = False

    # --- FFmpeg ---
    FFMPEG_BIN: str = "ffmpeg"
    FFPROBE_BIN: str = "ffprobe"
    # Longest time one ffmpeg/ffprobe call may run before we stop it.
    FFMPEG_TIMEOUT_SECONDS: float = 600.0

    # --- Intake (input checks) ---
    MAX_AUDIO_SECONDS: float = 600.0
    MAX_DOWNLOAD_MB: int = 100
    # Total time allowed to download audio_url (connect + all data).
    DOWNLOAD_TIMEOUT_SECONDS: float = 60.0
    # All audio is converted to WAV with this sample rate, mono.
    AUDIO_SAMPLE_RATE: int = 48000

    # --- Text to speech ---
    TTS_PROVIDER: Literal["kokoro", "mock"] = "mock"
    KOKORO_REPO_ID: str = "hexgrad/Kokoro-82M"
    KOKORO_SPEED: float = 1.0
    # Our voice name = kokoro language code : kokoro voice id. Comma-separated.
    # Kokoro has no Indian-English voice: en-IN uses a Hindi speaker voice reading English.
    TTS_VOICE_MAP: str = (
        "en-US-female=a:af_heart,en-US-male=a:am_michael,"
        "en-GB-female=b:bf_emma,en-GB-male=b:bm_george,"
        "en-IN-female=a:hf_alpha,en-IN-male=a:hm_omega,"
        "hi-IN-female=h:hf_alpha,hi-IN-male=h:hm_omega"
    )
    # Finished TTS audio is kept here and reused for the same text + voice + model + speed. Empty = off.
    TTS_CACHE_DIR: str = "./data/tts_cache"
    # Scene-list input: silence added after each scene's narration.
    SCENE_PAUSE_SECONDS: float = 0.3
    # Mock TTS speaking speed (the length of its tone).
    MOCK_WORDS_PER_SECOND: float = 2.5

    # --- Transcription ---
    TRANSCRIBE_PROVIDER: Literal["whisper", "mock"] = "mock"
    WHISPER_MODEL: str = "small"
    WHISPER_DEVICE: str = "cpu"
    WHISPER_COMPUTE_TYPE: str = "int8"
    WHISPER_BEAM_SIZE: int = 5
    # Empty = detect the language automatically.
    WHISPER_LANGUAGE: str = ""
    # Skip parts without voice (Silero VAD, bundled with faster-whisper). Helps on music.
    WHISPER_VAD_FILTER: bool = True
    # Non-English narration: Whisper also translates the speech to English (a second, faster pass). The
    # English is used ONLY to search for footage; captions stay in the spoken language. false = search
    # with the original text (Groq still writes English queries; the offline rules then find few words).
    TRANSLATE_FOR_SEARCH: bool = True
    # Mock transcriber: words used for uploaded audio (it cannot really listen).
    MOCK_TRANSCRIPT: str = "This is a mock transcript. The mock adapter does not listen to the audio."

    # --- Speech checks ---
    # Loudest moment quieter than this (dB) = the file is silent.
    SILENCE_MAX_VOLUME_DB: float = -45.0
    # silencedetect: below this level (dB) for SILENCE_MIN_SECONDS counts as silence.
    SILENCE_NOISE_DB: float = -45.0
    SILENCE_MIN_SECONDS: float = 0.5
    # Less non-silent audio than this (seconds) = NO_SPEECH_DETECTED.
    MIN_SOUND_SECONDS: float = 0.5
    # Sound but fewer recognised words per minute of sound than this = MUSIC_ONLY_AUDIO.
    MUSIC_MIN_WORDS_PER_MINUTE: float = 20.0
    # Whisper's average "no speech" probability above this = MUSIC_ONLY_AUDIO.
    MUSIC_MAX_NO_SPEECH_PROB: float = 0.6
    # Average word confidence below this = MUSIC_ONLY_AUDIO.
    MUSIC_MIN_WORD_PROB: float = 0.3

    # --- Scenes ---
    MIN_SCENE_SECONDS: float = 2.0
    MAX_SCENE_SECONDS: float = 8.0
    # A gap between two words at least this long counts as a phrase pause.
    PAUSE_SECONDS: float = 0.35

    # --- Captions ---
    CAPTION_MAX_CHARS_PER_LINE: int = 42
    CAPTION_MAX_LINES: int = 2
    CAPTION_MAX_SECONDS: float = 6.0
    # Keep a caption on screen this long after its last word (unless the next one starts).
    CAPTION_LINGER_SECONDS: float = 0.4
    CAPTION_FONT: str = "Arial"
    # Caption language (Whisper code) -> font for that script. A caption whose language is not listed but
    # whose text holds Tamil / Devanagari letters gets that script's font too. Others use CAPTION_FONT.
    CAPTION_FONTS_BY_LANGUAGE: str = (
        "ta=Noto Sans Tamil,hi=Noto Sans Devanagari,mr=Noto Sans Devanagari,ne=Noto Sans Devanagari"
    )
    # Size factor per font, so every script looks about as big as CAPTION_FONT at the same size. Noto Sans
    # Devanagari has tall line metrics: at one size its letters were 28 px high vs 46 px for Arial (measured).
    CAPTION_FONT_SCALES: str = "Noto Sans Devanagari=1.4"
    # Folder of font files given to FFmpeg's subtitles filter (fontsdir). Empty = app/fonts (Noto, SIL OFL).
    CAPTION_FONTS_DIR: str = ""

    # --- Render ---
    RENDER_FPS: int = 30
    # libx264 speed/size trade-off (ultrafast ... veryslow) and quality (lower = better).
    VIDEO_PRESET: str = "veryfast"
    VIDEO_CRF: int = 23
    AUDIO_BITRATE: str = "192k"
    # Scene segments rendered at the same time (each is its own ffmpeg). 1 = one after the other.
    RENDER_WORKERS: int = 3

    # --- Reproducibility ---
    # One seed for everything random-ish: LLM sampling, mock providers, card colours.
    SEED: int = 1234

    # --- Network safety (SSRF) ---
    # false = refuse audio_url / footage URLs that point to private, loopback or link-local
    # addresses (e.g. 127.0.0.1, 10.x, 169.254.169.254). Only tests set this to true.
    ALLOW_PRIVATE_URLS: bool = False

    # --- Scene understanding (meaning -> search queries + flags) ---
    UNDERSTAND_PROVIDER: Literal["groq", "rules", "mock"] = "mock"
    GROQ_API_KEY: str = ""
    GROQ_BASE_URL: str = "https://api.groq.com/openai/v1"
    # gpt-oss is Apache-2.0 licensed; any Groq chat model that supports JSON mode works.
    LLM_MODEL: str = "openai/gpt-oss-120b"
    LLM_TIMEOUT_SECONDS: float = 30.0
    # 0 = always pick the most likely words, so the same script gives (nearly) the same answer.
    LLM_TEMPERATURE: float = 0.0
    # Extra tries after the first failed LLM call.
    LLM_RETRIES: int = 1
    # Scenes sent to the LLM in one request. Long scripts are split into batches, so one answer never gets
    # too big for the per-minute token limit (Groq free tier: 8,000 tokens per minute for this model).
    # 6: with 12 the answer was cut short (scenes 8-11 missing) in 2 of 3 real runs on 2026-10-17.
    LLM_BATCH_SIZE: int = 6
    # HTTP 429: wait the server's retry-after (capped at this many seconds per wait) and try again. All the
    # waits of one job together may not pass LLM_MAX_WAIT_SECONDS; after that the rest uses the rules.
    LLM_MAX_RETRY_AFTER_SECONDS: float = 30.0
    LLM_MAX_WAIT_SECONDS: float = 120.0
    # gpt-oss "reasoning_effort" (low | medium | high). Empty = the provider's default.
    LLM_REASONING_EFFORT: str = ""
    # Search queries per scene (meaning, metaphor, setting, action, mood).
    QUERIES_PER_SCENE: int = 5
    # A whole story counts as sensitive (calm, respectful footage for EVERY scene) when at least this many
    # of its scenes are sensitive, or at least STORY_SENSITIVE_MIN_SHARE of them, or the LLM says so.
    STORY_SENSITIVE_MIN_SCENES: int = 2
    STORY_SENSITIVE_MIN_SHARE: float = 0.3

    # --- Quality mode: how hard the footage search tries (speed vs. thoroughness) ---
    # fast | balanced | best. It fills in the settings in QUALITY_PRESETS below; a setting you set yourself
    # (env / .env) always wins over the mode. fast also switches the VLM judge off.
    QUALITY_MODE: Literal["fast", "balanced", "best"] = "balanced"

    # --- Footage providers ---
    # Tried in this order for every scene. Names: pixabay_video, pixabay_image_motion,
    # wikimedia, pexels, generated, mock. A provider without its API key is skipped.
    PROVIDER_ORDER: str = "pixabay_video,pixabay_image_motion,wikimedia,openverse,generated"
    # Comma-separated providers to switch off (e.g. "pixabay_video").
    PROVIDERS_DISABLED: str = ""
    # JSON file re-read before every search, so a provider can be switched off DURING a job:
    # {"disabled": ["pixabay_video"]}
    PROVIDERS_OVERRIDE_FILE: str = "./data/providers_override.json"
    PROVIDER_TIMEOUT_SECONDS: float = 15.0
    # Results asked for per search query, and how many result pages. Where the API allows a bigger page,
    # the pages are fetched as ONE request of RESULTS x PAGES results (the same results, fewer API calls).
    PROVIDER_RESULTS_PER_QUERY: int = 8
    PROVIDER_PAGES: int = 1
    # At most this many candidates of one source are scored per scene (CLIP is the slow part: ~25 pictures/s
    # on an 8-core laptop CPU). They are taken round-robin over the queries (every query's 1st result, then
    # every query's 2nd ...), so each query still brings its best hits. 0 = no cap.
    MAX_CANDIDATES_PER_SOURCE: int = 15
    # Pass 1 also downloads + checks each scene's likely winner, in parallel (same choices, less waiting).
    PREFETCH_CLIPS: bool = True
    # Early exit: score a source's candidates EARLY_EXIT_STEP at a time and stop as soon as one CLEARLY passes
    # (score >= STOCK_MIN_SCORE + PREFER_VIDEO_MARGIN and not rejected by the logo check). The rest is scored
    # later only if pass 2 needs it (that clip already used, a failed download, a QA retry, a clip swap).
    EARLY_EXIT: bool = True
    EARLY_EXIT_STEP: int = 6
    # At most this many of a scene's queries per source ("name=n,..."); sources not listed use all of them.
    # Openverse allows only 20 anonymous searches per minute and 200 per day.
    PROVIDER_MAX_QUERIES: str = "wikimedia=3,openverse=2"
    # Client-side request limits per minute ("name=n,..."), so a long job waits instead of getting HTTP 429.
    # Pixabay: 100 requests per 60 s per key; Openverse (no key): 20 per minute.
    PROVIDER_REQUESTS_PER_MINUTE: str = "pixabay=90,openverse=18"
    # After a 429 answer a source is skipped until its retry-after has passed (at least this many seconds).
    PROVIDER_COOLDOWN_SECONDS: float = 60.0
    OPENVERSE_API_URL: str = "https://api.openverse.org/v1/images/"

    # --- Footage speed (same choices, less waiting) ---
    # Scenes whose footage is searched + scored at the same time (pass 1). The choice itself (pass 2)
    # is always made one scene at a time, in scene order, so the no-repeat rule and the result stay
    # the same. 1 = one scene after the other.
    FOOTAGE_WORKERS: int = 4
    # Preview pictures downloaded at the same time (per scene), and the time limit for one.
    PREVIEW_WORKERS: int = 8
    PREVIEW_TIMEOUT_SECONDS: float = 8.0
    # Search results + preview pictures are kept this long and reused by later jobs with the same
    # query. Pixabay's API terms say "requests must be cached for 24 hours", hence 86400. Within one job they
    # are always reused. 0 = off.
    FOOTAGE_CACHE_SECONDS: int = 86400
    FOOTAGE_CACHE_DIR: str = "./data/footage_cache"
    # Pictures CLIP looks at in one go.
    CLIP_BATCH_SIZE: int = 32
    # Load the CLIP model when the worker starts (not inside the first job).
    CLIP_PRELOAD: bool = True
    PIXABAY_API_KEY: str = ""
    PIXABAY_VIDEO_API_URL: str = "https://pixabay.com/api/videos/"
    PIXABAY_IMAGE_API_URL: str = "https://pixabay.com/api/"
    PEXELS_API_KEY: str = ""
    PEXELS_VIDEO_API_URL: str = "https://api.pexels.com/videos/search"
    WIKIMEDIA_API_URL: str = "https://commons.wikimedia.org/w/api.php"
    # Wikimedia requires a contact (email or URL) here, or it answers 403 (provider is then skipped).
    WIKIMEDIA_USER_AGENT: str = "broll-engine/0.3 (set WIKIMEDIA_USER_AGENT to include your email or project URL)"
    # Largest footage file we download, and how long one download may take.
    MAX_CLIP_DOWNLOAD_MB: int = 80
    CLIP_DOWNLOAD_TIMEOUT_SECONDS: float = 60.0

    # --- Ranking ---
    # "clip" = open_clip scores preview images against the scene meaning; "mock" = word overlap (tests).
    RANK_SCORER: Literal["clip", "mock"] = "mock"
    CLIP_MODEL: str = "ViT-B-32"
    CLIP_PRETRAINED: str = "laion2b_s34b_b79k"
    # Use a stock clip only if its CLIP score is at least this.
    STOCK_MIN_SCORE: float = 0.22
    # Small extra points (only used to order candidates, never to pass the threshold).
    DURATION_BONUS: float = 0.02
    ORIENTATION_BONUS: float = 0.02
    # Lines with a number/percentage always SHOW the number (text from the narration only, cards.py).
    GENERATED_FOR_STATISTICS: bool = True
    # How: true = look for real footage of the line's topic first and draw the number on it as a badge
    # (lower third); a plain number card only if no clip passes the rule. false = straight to a number card.
    STATISTIC_OVERLAY: bool = True
    # CLIP scoring: every query is encoded with these prompts ("|"-separated, {q} = the query) and the text
    # embeddings are averaged (prompt ensembling). A candidate's score = the mean of its best
    # SCORE_TOP_QUERIES query similarities (3 keeps the same scale as the old "mean of 3 queries").
    CLIP_PROMPTS: str = "a stock video of {q}|a photo of {q}|{q}"
    SCORE_TOP_QUERIES: int = 3
    # More frames per candidate: the best MULTI_FRAME_TOP_K video candidates of each source (by thumbnail score)
    # also get frames at MULTI_FRAME_POSITIONS of their smallest rendition (a ~1 MB file); their score is
    # then the mean over thumbnail + frames. 0 = thumbnail only.
    MULTI_FRAME_TOP_K: int = 2
    MULTI_FRAME_POSITIONS: str = "0.3,0.7"
    MULTI_FRAME_MAX_MB: int = 15
    # Text-heavy pictures (slides, screenshots, posters): CLIP probability over TEXT_PROMPTS vs
    # LOGO_NEUTRAL_PROMPTS; above TEXT_PENALTY_PROB the ORDER score loses TEXT_PENALTY (never the threshold).
    TEXT_PROMPTS: str = ("a screenshot of a document full of text|a presentation slide with text|"
                         "a poster with large printed words|a page of a printed newspaper")
    TEXT_PENALTY_PROB: float = 0.5
    TEXT_PENALTY: float = 0.03
    # A stock VIDEO that passes STOCK_MIN_SCORE is preferred over photo-motion: a photo wins only if its order
    # score is more than PREFER_VIDEO_MARGIN higher. A video scoring STOCK_MIN_SCORE + PREFER_VIDEO_MARGIN or
    # more is taken without searching photos (saves API calls).
    PREFER_VIDEO_MARGIN: float = 0.03
    # Adaptive threshold: if NO candidate of any source passes STOCK_MIN_SCORE, the best ones scoring at least
    # STOCK_MIN_SCORE x (1 - RELATIVE_MARGIN) may still be used, but ONLY if the independent VLM judge rates
    # one VLM_ACCEPT_SCORE or more. Without a judge, no adaptive pass.
    RELATIVE_MARGIN: float = 0.12
    # Borderline scenes: if the best candidate scores below BORDERLINE_SCORE, ONE more concrete query is written
    # (Groq, else the line's own words) and searched; its results are scored on the scene's original queries and
    # compete with the others. 0 = off.
    BORDERLINE_SCORE: float = 0.25
    # No plain card while usable footage exists: if nothing passes (not even the adaptive pass), the best clip
    # scoring at least BEST_EFFORT_MIN_SCORE is used with the line's keyword / number as a badge. 0 = off.
    BEST_EFFORT_MIN_SCORE: float = 0.17

    # --- Independent vision-language judge (optional) ---
    # "groq" = a vision model on Groq rates the top candidates 1-5 against the narration; "mock" = tests;
    # "none" = off. Off by default: it uses Groq's per-minute token budget and the model is a Groq preview.
    VLM_PROVIDER: Literal["groq", "mock", "none"] = "none"
    VLM_MODEL: str = "qwen/qwen3.8-27b"
    VLM_TIMEOUT_SECONDS: float = 30.0
    # Candidates judged per scene: drawn side by side as ONE picture (Groq counts 2,048 tokens per image).
    VLM_TOP_K: int = 3
    VLM_PANEL_WIDTH: int = 256
    # At most this many scenes judged per job (most uncertain first). 0 = every scene.
    VLM_MAX_SCENES_PER_JOB: int = 3
    # Also judge "confident" scenes (best score >= STOCK_MIN_SCORE + PREFER_VIDEO_MARGIN)? false = only close
    # calls and near misses (below the threshold) are judged.
    VLM_JUDGE_CONFIDENT: bool = False
    # 1-5 rating: <= VLM_REJECT_SCORE -> the candidate is dropped; >= VLM_ACCEPT_SCORE -> allows the adaptive pass.
    VLM_REJECT_SCORE: int = 1
    VLM_ACCEPT_SCORE: int = 4
    # HTTP 429: wait the retry-after if it is at most this long, else stop judging for this job (warning) and
    # keep the CLIP order. 0 = never wait: the first 429 switches the judge off for the rest of the job.
    VLM_MAX_RETRY_AFTER_SECONDS: float = 0.0
    # Logo check: logo probability = CLIP softmax over LOGO_PROMPTS + LOGO_NEUTRAL_PROMPTS ("|"-separated),
    # summed over the logo prompts (0 = surely clean, 1 = surely a logo), taken as the MAX over the whole
    # picture and a LOGO_CROP_GRID x LOGO_CROP_GRID grid of crops (small signs / laptop logos).
    # It does not depend on the scene text, so it means the same for every scene. Only candidates that
    # pass STOCK_MIN_SCORE are checked. prob > LOGO_REJECT_PROB -> rejected; prob > LOGO_PENALTY_PROB ->
    # kept, but LOGO_PENALTY is taken off its ORDER score (never changes whether it passes STOCK_MIN_SCORE).
    # Each neutral prompt is the same subject WITHOUT the logo ("a photo of a laptop" vs "a photo of a
    # laptop with an apple logo"); prompts that only say "no logos" did not work (CLIP ignores "no").
    # Calibrated on 82 + 40 (held-out) hand-labelled real Pixabay video thumbnails, see
    # testpack/calibration/logo_report.md (tools/logo_calibration.py repeats it).
    LOGO_CHECK: bool = True
    LOGO_PROMPTS: str = ("a photo of a laptop with an apple logo|a photo of a shop sign with a brand name|"
                         "a photo of advertising billboards|a photo of a car covered in sponsor logos|"
                         "a photo of a product with a printed brand name")
    LOGO_NEUTRAL_PROMPTS: str = ("a photo of a laptop|a photo of a shop|a photo of a city street|a photo of a car|"
                                 "a photo of a product|a photo of a phone|a photo of people|a photo of nature|"
                                 "a photo of food|a close-up photo")
    LOGO_CROP_GRID: int = 3
    LOGO_REJECT_PROB: float = 0.6
    LOGO_PENALTY_PROB: float = 0.4
    LOGO_PENALTY: float = 0.03
    # After download, this many frames of the REAL clip (spread over its length) get the same
    # logo test; one frame above LOGO_REJECT_PROB rejects the clip.
    LOGO_FRAME_COUNT: int = 3

    # --- Clip fitting + look ---
    # A clip shorter than its scene may be slowed down by up to this fraction, then it loops.
    MAX_SLOWDOWN: float = 0.25
    # Ken Burns (slow zoom) on still images: final zoom factor.
    KEN_BURNS_ZOOM: float = 1.12
    # Frames sampled per clip to find the most interesting region for cropping.
    SALIENCY_SAMPLE_FRAMES: int = 5
    # Subject-aware crop (only with RANK_SCORER=clip): CROP_SAMPLE_FRAMES colour frames, each
    # candidate crop window (left / centre / right / salient) is scored with CLIP against the
    # scene meaning. If the best window scores CROP_BLUR_MARGIN or more BELOW the whole frame,
    # no window keeps the subject -> blurred-background fit instead of a crop.
    CROP_SAMPLE_FRAMES: int = 3
    CROP_BLUR_MARGIN: float = 0.02
    # Leave the salient / centre window only if another window scores more than this above it.
    # (Real run 2026-10-16: an edge window beat the correct centred subject by 0.024 - CLIP noise.)
    CROP_TIE_MARGIN: float = 0.03
    # Near-tie windows that start or end INSIDE the subject lose to a near-tie window that keeps it whole.
    # Subject = where detail is LOCALISED (a few columns of a row much busier than the rest of that row: a
    # boat on a calm sea; waves or grass are busy everywhere and do not count). Its span = the peak
    # +- CROP_SUBJECT_PAD (fraction of the frame), widened to where localised detail stays >= CROP_SUBJECT_LEVEL
    # x the peak. (Real case, run 1: a 9:16 sailboat clip cut at the hull by a near-tie window.)
    CROP_SUBJECT_LEVEL: float = 0.5
    CROP_SUBJECT_PAD: float = 0.06
    # Brightness / colour normalisation targets (signalstats YAVG 0-255, SATAVG) and limits.
    # Real Pixabay clips measured YAVG 102-107 and SATAVG 15-17 in the first real run.
    TARGET_LUMA: float = 110.0
    MAX_BRIGHTNESS_SHIFT: float = 0.12
    TARGET_SATURATION: float = 18.0

    # --- Quality checks after render ---
    QA_BLACK_SECONDS: float = 0.5
    QA_FREEZE_SECONDS: float = 2.0
    QA_MAX_DRIFT_SECONDS: float = 0.1
    # How many other clips we try for a scene whose segment fails QA.
    QA_RETRIES: int = 1

    # Small helpers so other code does not split strings itself.
    @property
    def api_tokens(self) -> list[str]:
        return [t.strip() for t in self.API_TOKENS.split(",") if t.strip()]

    @property
    def allowed_audio_extensions(self) -> set[str]:
        return {e.strip().lower().lstrip(".") for e in self.ALLOWED_AUDIO_EXTENSIONS.split(",") if e.strip()}

    @property
    def provider_order(self) -> list[str]:
        return [p.strip().lower() for p in self.PROVIDER_ORDER.split(",") if p.strip()]

    @property
    def providers_disabled(self) -> set[str]:
        return {p.strip().lower() for p in self.PROVIDERS_DISABLED.split(",") if p.strip()}

    @property
    def logo_prompts(self) -> list[str]:
        return [p.strip() for p in self.LOGO_PROMPTS.split("|") if p.strip()]

    @property
    def logo_neutral_prompts(self) -> list[str]:
        return [p.strip() for p in self.LOGO_NEUTRAL_PROMPTS.split("|") if p.strip()]

    @model_validator(mode="after")
    def _apply_quality_mode(self) -> "Settings":
        """Fill in the QUALITY_MODE preset, except for settings given explicitly (env / .env / arguments)."""
        for name, value in QUALITY_PRESETS[self.QUALITY_MODE].items():
            if name not in self.model_fields_set:
                object.__setattr__(self, name, value)
        return self

    @property
    def vlm_enabled(self) -> bool:
        """The independent judge runs only if VLM_PROVIDER names one and the mode is not fast."""
        return self.VLM_PROVIDER != "none" and self.QUALITY_MODE != "fast"

    @property
    def text_prompts(self) -> list[str]:
        return [p.strip() for p in self.TEXT_PROMPTS.split("|") if p.strip()]

    @property
    def clip_prompts(self) -> list[str]:
        prompts = [p.strip() for p in self.CLIP_PROMPTS.split("|") if p.strip()]
        return [p if "{q}" in p else p + " {q}" for p in prompts] or ["a photo of {q}"]

    @property
    def multi_frame_positions(self) -> list[float]:
        return [float(x) for x in self.MULTI_FRAME_POSITIONS.split(",") if x.strip()]

    @staticmethod
    def _name_numbers(text: str) -> dict[str, float]:
        """"wikimedia=3,openverse=2" -> {"wikimedia": 3.0, "openverse": 2.0}"""
        out = {}
        for item in text.split(","):
            name, _, value = item.partition("=")
            if name.strip() and value.strip():
                out[name.strip().lower()] = float(value)
        return out

    @property
    def provider_max_queries(self) -> dict[str, int]:
        return {k: int(v) for k, v in self._name_numbers(self.PROVIDER_MAX_QUERIES).items()}

    @property
    def provider_requests_per_minute(self) -> dict[str, float]:
        return self._name_numbers(self.PROVIDER_REQUESTS_PER_MINUTE)

    @property
    def tts_voices(self) -> dict[str, tuple[str, str]]:
        """"en-US-female=a:af_heart,..." -> {"en-US-female": ("a", "af_heart"), ...}"""
        voices = {}
        for item in self.TTS_VOICE_MAP.split(","):
            name, _, target = item.strip().partition("=")
            lang, _, voice_id = target.partition(":")
            if name and lang and voice_id:
                voices[name.strip()] = (lang.strip(), voice_id.strip())
        return voices

    @property
    def caption_fonts_by_language(self) -> dict[str, str]:
        """"ta=Noto Sans Tamil,hi=..." -> {"ta": "Noto Sans Tamil", "hi": ...}"""
        fonts = {}
        for item in self.CAPTION_FONTS_BY_LANGUAGE.split(","):
            lang, _, font = item.partition("=")
            if lang.strip() and font.strip():
                fonts[lang.strip().lower()] = font.strip()
        return fonts

    @property
    def caption_font_scales(self) -> dict[str, float]:
        """"Noto Sans Devanagari=1.4" -> {"Noto Sans Devanagari": 1.4}"""
        scales = {}
        for item in self.CAPTION_FONT_SCALES.split(","):
            font, _, factor = item.partition("=")
            if font.strip() and factor.strip():
                scales[font.strip()] = float(factor)
        return scales


# What each QUALITY_MODE sets (balanced = the defaults above; best = the full Session 7B search).
QUALITY_PRESETS: dict[str, dict] = {
    "fast": {"QUERIES_PER_SCENE": 3, "PROVIDER_PAGES": 1, "MAX_CANDIDATES_PER_SOURCE": 10, "EARLY_EXIT": True,
             "MULTI_FRAME_TOP_K": 0, "VLM_TOP_K": 2, "VLM_MAX_SCENES_PER_JOB": 0, "VLM_JUDGE_CONFIDENT": False},
    "balanced": {"QUERIES_PER_SCENE": 5, "PROVIDER_PAGES": 1, "MAX_CANDIDATES_PER_SOURCE": 15, "EARLY_EXIT": True,
                 "MULTI_FRAME_TOP_K": 2, "VLM_TOP_K": 3, "VLM_MAX_SCENES_PER_JOB": 3, "VLM_JUDGE_CONFIDENT": False},
    "best": {"QUERIES_PER_SCENE": 5, "PROVIDER_PAGES": 2, "MAX_CANDIDATES_PER_SOURCE": 48, "EARLY_EXIT": False,
             "MULTI_FRAME_TOP_K": 3, "VLM_TOP_K": 3, "VLM_MAX_SCENES_PER_JOB": 4, "VLM_JUDGE_CONFIDENT": True},
}


@lru_cache
def get_settings() -> Settings:
    """Build the settings once and reuse them. Tests call get_settings.cache_clear()."""
    return Settings()
