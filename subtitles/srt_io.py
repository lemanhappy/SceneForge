from __future__ import annotations

import re

from .models import SubtitleLine, SubtitleTrack


_TIMELINE_RE = re.compile(
    r"^(?P<start>\d{1,3}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*"
    r"(?P<end>\d{1,3}:\d{2}:\d{2}[,.]\d{1,3})"
)


def _seconds(value: str) -> float:
    hours, minutes, tail = value.replace(",", ".").split(":")
    return round(int(hours) * 3600 + int(minutes) * 60 + float(tail), 3)


def parse_srt_text(content: str) -> SubtitleTrack:
    """Parse standard SRT blocks into the project's subtitle model."""
    normalized = str(content or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return SubtitleTrack()
    parsed = []
    for block in re.split(r"\n\s*\n", normalized):
        rows = [row.rstrip() for row in block.split("\n")]
        timeline_index = next(
            (index for index, row in enumerate(rows) if _TIMELINE_RE.match(row.strip())),
            None,
        )
        if timeline_index is None:
            continue
        match = _TIMELINE_RE.match(rows[timeline_index].strip())
        text = "\n".join(rows[timeline_index + 1:]).strip()
        if not match or not text:
            continue
        parsed.append(SubtitleLine(
            text=text,
            start=_seconds(match.group("start")),
            end=_seconds(match.group("end")),
        ))
    return SubtitleTrack(lines=parsed)


def parse_srt(path: str) -> SubtitleTrack:
    with open(path, encoding="utf-8-sig") as handle:
        return parse_srt_text(handle.read())
