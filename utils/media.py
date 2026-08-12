import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def ffmpeg_executable() -> Optional[str]:
    """Return a usable FFmpeg executable, including the bundled fallback."""
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def probe_media_duration(path: str, *, ffmpeg: Optional[str] = None) -> float:
    """Read container duration without decoding the full media file."""
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(path)
    executable = ffmpeg or ffmpeg_executable()
    if not executable:
        raise RuntimeError("FFmpeg is required to inspect media duration")
    result = subprocess.run(
        [executable, "-hide_banner", "-i", str(source), "-t", "0", "-f", "null", "-"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    match = _DURATION_RE.search(result.stderr or "")
    if not match:
        raise ValueError(f"Could not determine media duration: {path}")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def media_has_audio(path: str, *, ffmpeg: Optional[str] = None) -> bool:
    executable = ffmpeg or ffmpeg_executable()
    if not executable:
        raise RuntimeError("FFmpeg is required to inspect media streams")
    result = subprocess.run(
        [executable, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    return bool(re.search(r"Stream #\S+.*: Audio:", result.stderr or ""))
