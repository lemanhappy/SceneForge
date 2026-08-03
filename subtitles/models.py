from pydantic import BaseModel, Field
from typing import List, Optional


class SubtitleLine(BaseModel):
    """A single spoken line tied to a shot (timing filled in by the timeline)."""

    text: str
    speaker: Optional[str] = None
    shot_idx: Optional[int] = None
    # absolute timeline seconds; populated by build_timeline
    start: float = 0.0
    end: float = 0.0


class SubtitleTrack(BaseModel):
    lines: List[SubtitleLine] = Field(default_factory=list)

    def __len__(self) -> int:
        return len(self.lines)


class SubtitleStyle(BaseModel):
    font_path: Optional[str] = None
    fallback_fonts: List[str] = Field(default_factory=lambda: ["Microsoft YaHei", "SimHei", "Noto Sans CJK SC"])
    font_size: int = 42
    primary_color: str = "white"
    outline_color: str = "black"
    outline_width: int = 2
    position: str = "bottom"
    # Vertical margin from the aligned edge (px in the .ass PlayRes space).
    margin_v: int = 30
