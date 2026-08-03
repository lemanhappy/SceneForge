"""Deterministic compilation of structured storyboard data for video models."""

from __future__ import annotations

from typing import Any, Iterable


def _value(obj: Any, name: str, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _items(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, Iterable):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _seconds(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    return str(int(number)) if number.is_integer() else f"{number:.1f}"


def compile_video_prompt(shot: Any) -> str:
    """Build the complete visual prompt for a shot without dropping constraints.

    Spoken dialogue is intentionally excluded. It is rendered separately as audio
    and subtitles; sending literal dialogue to a video model often creates unwanted
    text in the image.
    """

    duration = _value(shot, "duration_sec", 5.0) or 5.0
    motion = str(_value(shot, "motion_desc", "") or "").strip()
    visual = str(_value(shot, "visual_desc", "") or "").strip()
    parts = [f"Single continuous shot. Planned duration: {_seconds(duration)} seconds."]
    if motion or visual:
        parts.append(f"Core visual motion:\n{motion or visual}")

    beats = list(_value(shot, "beats", []) or [])
    beat_lines = []
    for beat in sorted(beats, key=lambda item: float(_value(item, "start_sec", 0) or 0)):
        start = _value(beat, "start_sec", 0)
        end = _value(beat, "end_sec", duration)
        details = []
        camera = str(_value(beat, "camera", "") or "").strip()
        action = str(_value(beat, "action", "") or "").strip()
        performance = str(_value(beat, "performance", "") or "").strip()
        if camera:
            details.append(f"Camera: {camera}")
        if action:
            details.append(f"Action: {action}")
        if performance:
            details.append(f"Performance: {performance}")
        if details:
            beat_lines.append(f"{_seconds(start)}-{_seconds(end)}s: " + "; ".join(details) + ".")
    if beat_lines:
        parts.append("Timed performance beats (relative to this shot; preserve their order and pacing):\n" + "\n".join(beat_lines))

    style = _items(_value(shot, "visual_style", []))
    if style:
        parts.append("Visual style: " + "; ".join(style) + ".")
    avoid = _items(_value(shot, "avoid", []))
    if avoid:
        parts.append("Avoid: " + "; ".join(avoid) + ".")
    return "\n\n".join(parts)
