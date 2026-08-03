import re
from typing import List, Optional

from .models import SubtitleLine

# A bracketed tag at the start of a line: [Speaker] / [Sound Effect] / [Narrator] / [王云宝] ...
_TAG_RE = re.compile(r"^\s*\[(?P<tag>[^\]]+)\]\s*(?P<rest>.*)$")
# After a "[Speaker]" tag: "name (emotion): text"
_NAME_EMO_RE = re.compile(r"^(?P<name>[^():：]+?)\s*(?:\((?P<emotion>[^)]*)\))?\s*[:：]\s*(?P<text>.+?)\s*$")
# After a "[name]" tag (name already captured): "(emotion): text" / ": text" /
# "text" (colon optional — the model sometimes writes [Narrator] "line").
_EMO_TEXT_RE = re.compile(r"^\s*(?:\((?P<emotion>[^)]*)\))?\s*[:：]?\s*(?P<text>.+?)\s*$")
# audio_desc often packs several segments on one line, each starting with a tag:
#   [Sound Effect] hum. [王云宝] (emotion): "台词"
_SEGMENT_SPLIT_RE = re.compile(r"(?=\[[^\]]+\])")
# A non-Speaker tag is often followed by the speaker name in angle brackets, e.g.
#   [Inner Monologue] <wangyunbao> (emotion): line   /   [Narrator] <name>: line
# Strip that leading "<name>" so the English name never leaks into the subtitle.
_LEADING_ANGLE_NAME_RE = re.compile(r"^\s*<(?P<name>[^>]+)>\s*(?P<rest>.*)$")
# Narration: "..."  or  X says: "..."  in motion_desc
_QUOTED_RE = re.compile(r'[“"]([^”"]+)[”"]')


def _clean(text: str) -> str:
    return text.strip().strip('“”"').strip()


def _parse_spoken_line(raw: str, shot_idx) -> Optional[SubtitleLine]:
    """Parse one audio_desc line into a spoken SubtitleLine, or None.

    Handles both the documented ``[Speaker] name (emotion): line`` convention and
    the form the model actually tends to emit — ``[Narrator]: line`` or
    ``[王云宝] (emotion): line`` (speaker name inside the brackets).
    ``[Sound Effect] ...`` never produces a subtitle.
    """
    tag_match = _TAG_RE.match(raw)
    if not tag_match:
        return None
    tag = tag_match.group("tag").strip()
    rest = tag_match.group("rest")
    if tag.lower() == "sound effect":
        return None
    if tag.lower() == "speaker":
        m = _NAME_EMO_RE.match(rest)
        if not m:
            return None
        speaker, text = _clean(m.group("name")), _clean(m.group("text"))
    else:
        # The tag itself is the speaker label ([Narrator]/[Inner Monologue]/[王云宝]),
        # but the model sometimes also names the speaker in angle brackets right
        # after the tag. Strip that "<name>" first so it isn't baked into the line.
        speaker = tag
        name_match = _LEADING_ANGLE_NAME_RE.match(rest)
        if name_match:
            speaker = _clean(name_match.group("name")) or tag
            rest = name_match.group("rest")
        m = _EMO_TEXT_RE.match(rest)
        if not m:
            return None
        text = _clean(m.group("text"))
    if not text:
        return None
    return SubtitleLine(text=text, speaker=speaker or None, shot_idx=shot_idx)


def extract_spoken_content(shot_description) -> List[SubtitleLine]:
    """Extract subtitle lines for one shot.

    Priority:
    1. Structured ``audio_desc`` speaker entries — both ``[Speaker] name
       (emotion): line`` and ``[<name>] (emotion): line`` (e.g. ``[Narrator]:``).
       ``[Sound Effect]`` entries never produce subtitles.
    2. Fallback: quoted dialogue / narration inside ``motion_desc``.

    ``shot_description`` may be a ShotDescription model or any object exposing
    ``audio_desc`` / ``motion_desc`` / ``idx`` attributes.
    """
    shot_idx = getattr(shot_description, "idx", None)
    audio_desc = (getattr(shot_description, "audio_desc", None) or "")
    lines: List[SubtitleLine] = []

    # Split into bracketed segments first (one audio_desc line can hold several,
    # e.g. "[Sound Effect] hum. [王云宝] (emotion): line"), then parse each.
    for raw in _SEGMENT_SPLIT_RE.split(str(audio_desc)):
        raw = raw.strip()
        if not raw:
            continue
        line = _parse_spoken_line(raw, shot_idx)
        if line is not None:
            lines.append(line)

    if lines:
        return lines

    # Fallback: quoted segments inside motion_desc (dialogue or narration).
    motion_desc = str(getattr(shot_description, "motion_desc", None) or "")
    for quoted in _QUOTED_RE.findall(motion_desc):
        text = _clean(quoted)
        if text:
            lines.append(SubtitleLine(text=text, shot_idx=shot_idx))
    return lines
