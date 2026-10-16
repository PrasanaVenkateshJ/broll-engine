"""Shapes of the JSON the API accepts (Pydantic checks them for us)."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

SUPPORTED_ASPECT_RATIOS = ("16:9", "9:16", "1:1")
MAX_SCRIPT_CHARS = 20000
MAX_SCENES = 200
DEFAULT_VOICE = "en-US-female"


class SceneIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=2000)
    visual_hint: str | None = Field(default=None, max_length=500)


class JobOptions(BaseModel):
    """Options shared by JSON jobs and upload jobs (the multipart "options" field)."""

    model_config = ConfigDict(extra="forbid")  # unknown fields -> INVALID_INPUT

    voice: str = Field(default=DEFAULT_VOICE, max_length=64)
    aspect_ratios: list[str] = Field(default_factory=lambda: list(SUPPORTED_ASPECT_RATIOS), min_length=1)
    style: str | None = Field(default=None, max_length=200)
    avoid: list[str] = Field(default_factory=list, max_length=50)
    captions: bool = True
    callback_url: HttpUrl | None = None

    @field_validator("aspect_ratios")
    @classmethod
    def _check_ratios(cls, value: list[str]) -> list[str]:
        bad = [r for r in value if r not in SUPPORTED_ASPECT_RATIOS]
        if bad:
            # The text "unsupported aspect ratio" is how the API spots this case
            # and answers UNSUPPORTED_ASPECT_RATIO instead of INVALID_INPUT.
            raise ValueError(f"unsupported aspect ratio {bad}; allowed: {list(SUPPORTED_ASPECT_RATIOS)}")
        return list(dict.fromkeys(value))  # drop duplicates, keep order


class JobCreate(JobOptions):
    """Body of POST /api/v1/broll/jobs as JSON. Exactly ONE of audio_url / script / scenes."""

    audio_url: HttpUrl | None = None
    script: str | None = Field(default=None, max_length=MAX_SCRIPT_CHARS)
    scenes: list[SceneIn] | None = Field(default=None, min_length=1, max_length=MAX_SCENES)

    @model_validator(mode="after")
    def _exactly_one_input(self):
        given = [name for name in ("audio_url", "script", "scenes") if getattr(self, name) is not None]
        if len(given) != 1:
            raise ValueError(
                "provide exactly one input: audio_url, script or scenes "
                f"(or upload a file as multipart field 'audio'); got {given or 'none'}"
            )
        return self

    @property
    def input_type(self) -> Literal["audio_url", "script", "scenes"]:
        if self.audio_url is not None:
            return "audio_url"
        if self.script is not None:
            return "script"
        return "scenes"


class SwapRequest(BaseModel):
    """Body of POST /api/v1/broll/jobs/{job_id}/scenes/{scene_index}/swap. Exactly ONE way to choose:

    {"mode": "next_best"}                               -> the next clip by the selection rule (also: empty body)
    {"provider": "pixabay_video", "clip_id": "123"}     -> this clip from this footage source
    {"clip_url": "https://cdn.pixabay.com/..."}         -> this file (Pixabay / Wikimedia address), or any
       public address with "user_supplied": true + licence, licence_url, attribution, commercial_use: true
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["next_best"] | None = None
    provider: str | None = Field(default=None, max_length=32)
    clip_id: str | None = Field(default=None, min_length=1, max_length=200)
    clip_url: HttpUrl | None = None
    user_supplied: bool = False
    licence: str | None = Field(default=None, min_length=1, max_length=200)
    licence_url: HttpUrl | None = None
    attribution: str | None = Field(default=None, min_length=1, max_length=500)
    commercial_use: bool | None = None

    @model_validator(mode="after")
    def _exactly_one_choice(self):
        if (self.provider is None) != (self.clip_id is None):
            raise ValueError("provider and clip_id must be given together")
        given = [name for name, on in (("mode", self.mode is not None), ("provider+clip_id", self.provider is not None),
                                       ("clip_url", self.clip_url is not None)) if on]
        if len(given) > 1:
            raise ValueError(f"choose ONE of mode, provider+clip_id or clip_url; got {given}")
        licence_fields = (self.licence, self.licence_url, self.attribution, self.commercial_use)
        if self.user_supplied:
            if self.clip_url is None:
                raise ValueError("user_supplied needs clip_url")
            if any(v is None for v in licence_fields):
                raise ValueError("a user_supplied clip needs licence, licence_url, attribution and commercial_use")
            if self.commercial_use is not True:
                raise ValueError("a user_supplied clip must allow commercial use (commercial_use: true)")
        elif any(v is not None for v in licence_fields):
            raise ValueError("licence fields are only for user_supplied clips")
        return self

    @property
    def choice(self) -> Literal["next_best", "provider_clip", "clip_url"]:
        if self.provider is not None:
            return "provider_clip"
        if self.clip_url is not None:
            return "clip_url"
        return "next_best"
