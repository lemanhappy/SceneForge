from typing import Callable, List, Sequence, Tuple

from .models import SubtitleLine, SubtitleTrack


def probe_duration(video_path: str) -> float:
    """Real shot duration via moviepy (imported lazily so timeline math is
    testable without a video backend)."""
    from moviepy import VideoFileClip

    clip = VideoFileClip(video_path)
    try:
        return float(clip.duration)
    finally:
        clip.close()


def build_timeline(
    shots: Sequence[Tuple[str, List[SubtitleLine]]],
    duration_provider: Callable[[str], float] = probe_duration,
) -> SubtitleTrack:
    """Assign absolute start/end times to subtitle lines.

    ``shots`` is an ordered sequence of ``(video_path, lines)``. Each shot's
    lines are confined to that shot's time window; when a shot has several lines
    the window is split proportionally to text length (longer line -> more time),
    which reads better than an even split for mixed-length dialogue.
    """
    cursor = 0.0
    placed: List[SubtitleLine] = []
    for video_path, lines in shots:
        duration = max(0.0, float(duration_provider(video_path)))
        if lines and duration > 0:
            weights = [max(1, len(line.text)) for line in lines]
            total = sum(weights)
            t = cursor
            for line, weight in zip(lines, weights):
                segment = duration * weight / total
                line.start = round(t, 3)
                line.end = round(t + segment, 3)
                t += segment
                placed.append(line)
        cursor += duration
    return SubtitleTrack(lines=placed)
