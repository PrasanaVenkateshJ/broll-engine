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
    RATE_LIMIT_PER_MINUTE: int = 120

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

    # Small helpers so other code does not split strings itself.
    @property
    def api_tokens(self) -> list[str]:
        return [t.strip() for t in self.API_TOKENS.split(",") if t.strip()]

    @property
    def allowed_audio_extensions(self) -> set[str]:
        return {e.strip().lower().lstrip(".") for e in self.ALLOWED_AUDIO_EXTENSIONS.split(",") if e.strip()}

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
