"""Shapes of the JSON the API accepts (Pydantic checks them for us)."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

SUPPORTED_ASPECT_RATIOS = ("16:9", "9:16", "1:1")


class SceneIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=2000)
    visual_hint: str | None = Field(default=None, max_length=500)


class JobOptions(BaseModel):
    """Options shared by JSON jobs and upload jobs (the multipart "options" field)."""

    model_config = ConfigDict(extra="forbid")  # unknown fields -> INVALID_INPUT

    voice: str = Field(default="en-US-female", max_length=64)
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
    script: str | None = Field(default=None, max_length=20000)
    scenes: list[SceneIn] | None = Field(default=None, min_length=1, max_length=200)

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
