from typing import Callable, List, Optional, Sequence, Tuple

from .extractor import extract_spoken_content
from .models import SubtitleLine, SubtitleStyle, SubtitleTrack
from .renderer import burn_in, render_ass, render_srt
from .timeline import build_timeline, probe_duration


class SubtitleService:
    """Facade over extraction -> timeline -> render -> burn-in (design §15)."""

    def __init__(self, style: Optional[SubtitleStyle] = None, enabled: bool = True, burn_in_enabled: bool = True):
        self.style = style or SubtitleStyle()
        self.enabled = enabled
        self.burn_in_enabled = burn_in_enabled

    @classmethod
    def from_config(cls, config: dict) -> Optional["SubtitleService"]:
        """Build from a pipeline config's ``subtitle`` section, or ``None`` when
        subtitles are disabled/unconfigured so the feature stays fully optional."""
        section = (config or {}).get("subtitle") or {}
        if not section.get("enabled"):
            return None
        style_section = section.get("style") or {}
        style = SubtitleStyle(
            font_path=section.get("font_path"),
            fallback_fonts=section.get("fallback_fonts") or SubtitleStyle().fallback_fonts,
            font_size=style_section.get("font_size", 42),
            primary_color=style_section.get("primary_color", "white"),
            outline_color=style_section.get("outline_color", "black"),
            outline_width=style_section.get("outline_width", 2),
            position=style_section.get("position", "bottom"),
            margin_v=style_section.get("margin_v", 30),
        )
        return cls(style=style, enabled=True, burn_in_enabled=bool(section.get("burn_in", True)))

    def extract_spoken_content(self, shot_description) -> List[SubtitleLine]:
        return extract_spoken_content(shot_description)

    def build_timeline(self, shots: Sequence[Tuple[str, List[SubtitleLine]]],
                       duration_provider: Callable[[str], float] = probe_duration) -> SubtitleTrack:
        return build_timeline(shots, duration_provider=duration_provider)

    def build_track(self, shot_descriptions: Sequence, video_paths: Sequence[str],
                    duration_provider: Callable[[str], float] = probe_duration) -> SubtitleTrack:
        """Convenience: extract per shot then lay out the full timeline.

        ``shot_descriptions`` and ``video_paths`` are parallel ordered sequences.
        """
        shots: List[Tuple[str, List[SubtitleLine]]] = []
        for shot_description, video_path in zip(shot_descriptions, video_paths):
            shots.append((video_path, self.extract_spoken_content(shot_description)))
        return self.build_timeline(shots, duration_provider=duration_provider)

    def render_ass(self, track: SubtitleTrack, path: str, hook: Optional[dict] = None,
                   label: Optional[dict] = None, screen_texts: Optional[List[dict]] = None) -> str:
        return render_ass(track, path, style=self.style, hook=hook, label=label,
                          screen_texts=screen_texts)

    def render_srt(self, track: SubtitleTrack, path: str) -> str:
        return render_srt(track, path)

    def burn_in(self, video_path: str, subtitle_path: str, output_path: str,
                metadata: Optional[dict] = None) -> Optional[str]:
        if not self.burn_in_enabled:
            return None
        return burn_in(video_path, subtitle_path, output_path, style=self.style, metadata=metadata)
