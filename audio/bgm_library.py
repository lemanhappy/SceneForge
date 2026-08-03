"""Background-music library: list built-in/uploaded tracks and accept custom
uploads. The *selection* (which track, on/off, volume) is persisted by the
server's BgmService into the pipeline config; this class only owns the files.
"""
import os
import re
from pathlib import Path
from typing import List, Optional

_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
_SAFE_RE = re.compile(r"[^0-9A-Za-z._\-一-鿿]+")


def _safe_name(filename: str) -> str:
    """Reduce an arbitrary upload name to a safe basename (no path parts)."""
    base = os.path.basename(str(filename or "").replace("\\", "/")).strip()
    stem, ext = os.path.splitext(base)
    stem = _SAFE_RE.sub("_", stem).strip("_") or "track"
    ext = ext.lower()
    if ext not in _AUDIO_EXTS:
        ext = ".mp3"
    return stem + ext


class BgmLibrary:
    def __init__(self, library_dir: str = "assets/bgm"):
        self.library_dir = Path(library_dir)

    def list_tracks(self) -> List[str]:
        if not self.library_dir.is_dir():
            return []
        return sorted(p.name for p in self.library_dir.iterdir()
                      if p.is_file() and p.suffix.lower() in _AUDIO_EXTS)

    def track_path(self, name: str) -> Optional[str]:
        """Absolute path of a library track, or None if it isn't a real file
        inside the library (guards against traversal via crafted names)."""
        if not name:
            return None
        candidate = (self.library_dir / _safe_name(name)).resolve()
        root = self.library_dir.resolve()
        if root != candidate.parent or not candidate.is_file():
            return None
        return str(candidate)

    def add_track(self, filename: str, data: bytes) -> str:
        """Save an uploaded track into the library; returns the stored name.
        A name collision is resolved by appending -1, -2, ... ."""
        self.library_dir.mkdir(parents=True, exist_ok=True)
        name = _safe_name(filename)
        stem, ext = os.path.splitext(name)
        target = self.library_dir / name
        i = 1
        while target.exists():
            target = self.library_dir / f"{stem}-{i}{ext}"
            i += 1
        target.write_bytes(data)
        return target.name
