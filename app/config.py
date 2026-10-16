"""All settings come from environment variables (or a local .env file).

Nothing else in the app should read os.environ directly: it asks get_settings().
Every field here is also listed, with a comment, in .env.example.
"""
from functools import lru_cache
from typing import Literal

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

    # --- Studio (the web UI at GET /, files under /studio/) ---
    STUDIO_ENABLED: bool = True
    # Folder with index.html, studio.css and the .js files. Empty = app/studio next to the code.
    STUDIO_DIR: str = ""
    # Sites allowed to show the Studio inside a frame (CSP frame-ancestors), space-separated, e.g.
    # "https://huggingface.co" for a Hugging Face Space page. Empty = no site may frame it.
    STUDIO_FRAME_ANCESTORS: str = ""

    # --- Database ---
    DATABASE_URL: str = "sqlite:///./data/broll.db"

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

    # --- Render ---
    RENDER_FPS: int = 30
    # libx264 speed/size trade-off (ultrafast ... veryslow) and quality (lower = better).
    VIDEO_PRESET: str = "veryfast"
    VIDEO_CRF: int = 23
    AUDIO_BITRATE: str = "192k"

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

    # --- Footage providers ---
    # Tried in this order for every scene. Names: pixabay_video, pixabay_image_motion,
    # wikimedia, pexels, generated, mock. A provider without its API key is skipped.
    PROVIDER_ORDER: str = "pixabay_video,pixabay_image_motion,wikimedia,generated"
    # Comma-separated providers to switch off (e.g. "pixabay_video").
    PROVIDERS_DISABLED: str = ""
    # JSON file re-read before every search, so a provider can be switched off DURING a job:
    # {"disabled": ["pixabay_video"]}
    PROVIDERS_OVERRIDE_FILE: str = "./data/providers_override.json"
    PROVIDER_TIMEOUT_SECONDS: float = 15.0
    # Results asked for per search query.
    PROVIDER_RESULTS_PER_QUERY: int = 8

    # --- Footage speed (same choices, less waiting) ---
    # Scenes whose footage is searched + scored at the same time (pass 1). The choice itself (pass 2)
    # is always made one scene at a time, in scene order, so the no-repeat rule and the result stay
    # the same. 1 = one scene after the other.
    FOOTAGE_WORKERS: int = 4
    # Preview pictures downloaded at the same time (per scene), and the time limit for one.
    PREVIEW_WORKERS: int = 8
    PREVIEW_TIMEOUT_SECONDS: float = 8.0
    # Search results + preview pictures are kept this long and reused by later jobs with the same
    # query (Pixabay asks API users to cache results). Within one job they are always reused. 0 = off.
    FOOTAGE_CACHE_SECONDS: int = 900
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
    # Lines with a number/percentage get a number card (text from the narration only) instead of stock footage.
    GENERATED_FOR_STATISTICS: bool = True
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


@lru_cache
def get_settings() -> Settings:
    """Build the settings once and reuse them. Tests call get_settings.cache_clear()."""
    return Settings()
