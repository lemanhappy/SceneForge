import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def _ffmpeg_exe() -> Optional[str]:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:  # moviepy ships imageio-ffmpeg
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def export_poster(video_path: str, out_path: str, at_seconds: float = 0.0,
                  runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                  ffmpeg: Optional[str] = None) -> Optional[str]:
    """Grab a single frame at ``at_seconds`` and save it as a poster/thumbnail
    image (format inferred from ``out_path``'s extension). Returns the output
    path, or ``None`` on any failure (missing ffmpeg, bad timestamp) so it never
    breaks a render."""
    exe = ffmpeg or _ffmpeg_exe()
    if exe is None:
        logger.warning("ffmpeg not found; skipping poster export for %s", video_path)
        return None
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    cmd = [exe, "-y", "-ss", str(max(0.0, float(at_seconds))), "-i", os.path.abspath(video_path),
           "-frames:v", "1", "-q:v", "2", os.path.abspath(out_path)]
    try:
        proc = runner(cmd, capture_output=True, text=True)
    except Exception as exc:  # pragma: no cover - environment dependent
        logger.warning("poster export failed to launch: %s", exc)
        return None
    if proc.returncode != 0 or not os.path.exists(out_path):
        logger.warning("poster export failed (code %s): %s", proc.returncode, (proc.stderr or "")[-300:])
        return None
    return out_path
