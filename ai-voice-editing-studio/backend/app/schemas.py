from pydantic import BaseModel, Field
from typing import Literal

class ProjectCreate(BaseModel):
    name: str = Field(default="Untitled voiceover", min_length=1, max_length=100)
    script_text: str = ""

class ScriptUpdate(BaseModel):
    script_text: str = Field(max_length=2_000_000)

class AnalysisRequest(BaseModel):
    language: Literal["auto", "bn", "en"] = "auto"

class SentenceDecision(BaseModel):
    include: bool
    take_start: float | None = Field(default=None, ge=0)
    take_end: float | None = Field(default=None, gt=0)

class ExportRequest(BaseModel):
    format: Literal["mp3", "wav"] = "mp3"
    bitrate: Literal["128k", "192k", "256k", "320k"] = "192k"
    sample_rate: Literal["22050", "44100", "48000"] = "44100"
    channels: Literal[1, 2] = 1
    normalize: bool = True
    noise_reduction: Literal["off", "light", "medium"] = "light"
    pause_mode: Literal["natural", "balanced", "tight"] = "balanced"
    filename: str = "edited-voiceover"
