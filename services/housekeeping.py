"""Disk housekeeping: reclaim space from a finished session's intermediate
artifacts (per-shot clips, frame PNGs, archived shot versions) while preserving
the deliverables (final videos, poster, subtitles, scripts).

Destructive and therefore explicit: cleanup removes the inputs needed to resume
or regenerate individual shots, so it's only run on user request (with a dry-run
``preview`` first). Everything is confined to the session's working dir.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Dict, List

# Deliverables that must never be deleted.
_KEEP_NAMES = {"poster.jpg"}
_KEEP_PREFIXES = ("final_video",)          # final_video.mp4 / _audio / _with_subtitles
_KEEP_SUFFIXES = (".json",)                # scripts / storyboard / characters / camera_tree


def _is_protected(path: Path) -> bool:
    name = path.name
    if name in _KEEP_NAMES or name.lower().endswith(_KEEP_SUFFIXES):
        return True
    return any(name.startswith(p) for p in _KEEP_PREFIXES)


def _reclaimable(working_dir: str) -> List[Path]:
    """Intermediate artifacts safe to delete once a final video exists:
    per-shot clips, frame images, and archived shot versions."""
    root = Path(working_dir)
    if not root.is_dir():
        return []
    targets: List[Path] = []
    # per-shot video clips + frame images (under any shots/<idx>/), and _archive dirs
    for shots_dir in root.rglob("shots"):
        if not shots_dir.is_dir():
            continue
        for shot in shots_dir.iterdir():
            if not shot.is_dir():
                continue
            archive = shot / "_archive"
            if archive.is_dir():
                targets.append(archive)
            for f in shot.iterdir():
                if f.is_file() and not _is_protected(f) and (f.suffix.lower() == ".png" or f.name == "video.mp4"):
                    targets.append(f)
    return targets


def _size(path: Path) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


class HousekeepingService:
    def has_final(self, working_dir: str) -> bool:
        root = Path(working_dir)
        return root.is_dir() and any(root.rglob("final_video*.mp4"))

    def preview(self, working_dir: str) -> Dict[str, Any]:
        targets = _reclaimable(working_dir)
        freeable = sum(_size(t) for t in targets)
        return {
            "items": len(targets),
            "freeable_bytes": freeable,
            "freeable_mb": round(freeable / (1024 * 1024), 2),
            "has_final": self.has_final(working_dir),
            "note": "清理将删除分镜中间文件(逐镜片段/帧图/历史版本)，保留成片/海报/字幕/剧本。删除后无法续跑或单镜重生成。",
        }

    def cleanup(self, working_dir: str, require_final: bool = True) -> Dict[str, Any]:
        if require_final and not self.has_final(working_dir):
            return {"ok": False, "error": "无成片，拒绝清理(避免删掉未完成产物)", "freed_bytes": 0}
        targets = _reclaimable(working_dir)
        freed = 0
        removed = 0
        for t in targets:
            sz = _size(t)
            try:
                if t.is_dir():
                    shutil.rmtree(t, ignore_errors=True)
                else:
                    os.remove(t)
                freed += sz
                removed += 1
            except OSError:
                pass
        return {"ok": True, "removed": removed, "freed_bytes": freed,
                "freed_mb": round(freed / (1024 * 1024), 2)}
