from typing import List, Optional

from pydantic import BaseModel, Field


class VoiceClip(BaseModel):
    """An audio clip (voiceover line or sound effect) placed on the final
    timeline. ``start`` is the absolute second offset (voiceover lines take it
    from the subtitle timeline so audio and subtitles stay aligned; sound
    effects take it from their shot's start). ``path`` points at the audio file.
    """

    path: str
    start: float = 0.0
    shot_idx: Optional[int] = None
    text: str = ""
    # Real audio length in seconds (measured after synthesis); drives subtitle
    # timing so voiceover and subtitles stay in lockstep.
    duration: float = 0.0


class AudioMixSpec(BaseModel):
    """Everything that goes into the final audio mix, assembled in one ffmpeg
    pass: voiceover lines, sound effects, an optional background-music bed, and
    optional loudness normalization."""

    voiceover: List[VoiceClip] = Field(default_factory=list)
    sfx: List[VoiceClip] = Field(default_factory=list)
    bgm_path: Optional[str] = None
    bgm_volume: float = 0.2
    bgm_ducking: bool = False
    bgm_duck_threshold: float = 0.02
    bgm_duck_ratio: float = 8.0
    bgm_duck_attack_ms: float = 20.0
    bgm_duck_release_ms: float = 350.0
    sfx_volume: float = 0.8
    loudnorm: bool = True
    loudnorm_i: float = -16.0
    loudnorm_tp: float = -1.5
    loudnorm_lra: float = 11.0
    mix_with_original: bool = False

    @property
    def has_added_audio(self) -> bool:
        """True when there is something to add beyond the original track; when
        False the caller skips re-muxing entirely."""
        return bool(self.voiceover or self.sfx or self.bgm_path)
