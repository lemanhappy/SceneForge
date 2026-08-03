import os
import re
from pathlib import Path
from typing import List, Optional

# "[Sound Effect] thunder rumbling" -> "thunder rumbling"
_SFX_RE = re.compile(r"^\s*\[Sound Effect\]\s*(?P<text>.+?)\s*$", re.IGNORECASE)
# audio_desc may pack several [Tag] segments on one line; split before each tag.
_SEGMENT_SPLIT_RE = re.compile(r"(?=\[[^\]]+\])")

_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}


def extract_sound_effects(shot_description) -> List[str]:
    """Return the ``[Sound Effect]`` descriptions for one shot (the subtitle
    extractor deliberately drops these; here we keep them to drive sfx)."""
    audio_desc = getattr(shot_description, "audio_desc", None) or ""
    effects: List[str] = []
    for seg in _SEGMENT_SPLIT_RE.split(str(audio_desc)):
        match = _SFX_RE.match(seg.strip())
        if match:
            text = match.group("text").strip()
            if text:
                effects.append(text)
    return effects


def _library_files(library_dir: str) -> List[Path]:
    root = Path(library_dir)
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_file() and p.suffix.lower() in _AUDIO_EXTS)


def resolve_sfx_file(text: str, library_dir: str) -> Optional[str]:
    """Best-effort match a sound-effect description to a file in ``library_dir``.

    Matches when a file's stem (split into words on ``_``/``-``/space) appears as
    a whole word in the description, case-insensitively. Returns the first match
    or ``None`` (no asset -> the effect is silently skipped, never fatal).
    """
    words = set(re.findall(r"[a-z0-9]+", (text or "").lower()))
    if not words:
        return None
    for path in _library_files(library_dir):
        stem_words = set(re.split(r"[\s_\-]+", path.stem.lower()))
        if stem_words & words:
            return str(path)
    return None
