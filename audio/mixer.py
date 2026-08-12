import logging
import os
import subprocess
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from .models import AudioMixSpec, VoiceClip

logger = logging.getLogger(__name__)


def probe_audio_duration(path: str) -> float:
    from utils.media import probe_media_duration

    return probe_media_duration(path)


def _ffmpeg_exe() -> Optional[str]:
    from utils.media import ffmpeg_executable

    return ffmpeg_executable()


def build_voiceover_filter(clips: Sequence[VoiceClip], mix_with_original: bool) -> str:
    """Build the ffmpeg ``-filter_complex`` graph that delays each clip to its
    start time and mixes them (optionally over the original audio at input 0).

    Inputs are: ``0`` = video, ``1..N`` = the voiceover clips in order. Returns a
    graph whose final pad is labelled ``[mix]``.
    """
    parts: List[str] = []
    labels: List[str] = []
    for i, clip in enumerate(clips, start=1):
        ms = max(0, int(round(clip.start * 1000)))
        parts.append(f"[{i}:a]adelay={ms}|{ms}[a{i}]")
        labels.append(f"[a{i}]")
    mix_inputs = (["[0:a]"] + labels) if mix_with_original else labels
    n = len(mix_inputs)
    parts.append(
        "".join(mix_inputs)
        + f"amix=inputs={n}:normalize=0:dropout_transition=0,apad[mix]"
    )
    return ";".join(parts)


def build_audio_inputs(spec: AudioMixSpec) -> List[tuple]:
    """Ordered ``(path, loop)`` list for ffmpeg ``-i`` (input 0 is the video).

    Order must match the indices used by :func:`build_audio_filter`:
    voiceover clips, then sfx clips, then the BGM bed (looped to cover the whole
    video).
    """
    inputs: List[tuple] = []
    for clip in spec.voiceover:
        inputs.append((clip.path, False))
    for clip in spec.sfx:
        inputs.append((clip.path, False))
    if spec.bgm_path:
        inputs.append((spec.bgm_path, True))
    return inputs


def build_audio_filter(spec: AudioMixSpec) -> str:
    """Build the ``-filter_complex`` graph for the combined audio mix.

    Voiceover/sfx clips are delayed to their start time; sfx and BGM get their
    own volume; everything is mixed (``amix``) and optionally loudness-normalized
    (``loudnorm``). The final pad is labelled ``[mix]``.
    """
    parts: List[str] = []
    voice_labels: List[str] = []
    sfx_labels: List[str] = []
    idx = 1  # input 0 is the video
    for clip in spec.voiceover:
        ms = max(0, int(round(clip.start * 1000)))
        parts.append(f"[{idx}:a]adelay={ms}|{ms}[v{idx}]")
        voice_labels.append(f"[v{idx}]")
        idx += 1
    for clip in spec.sfx:
        ms = max(0, int(round(clip.start * 1000)))
        parts.append(f"[{idx}:a]adelay={ms}|{ms},volume={spec.sfx_volume}[x{idx}]")
        sfx_labels.append(f"[x{idx}]")
        idx += 1

    labels: List[str] = []
    duck_bgm = bool(spec.bgm_path and spec.bgm_ducking and voice_labels)
    if duck_bgm:
        if len(voice_labels) == 1:
            parts.append(f"{voice_labels[0]}apad[voicebus]")
        else:
            parts.append(
                "".join(voice_labels)
                + f"amix=inputs={len(voice_labels)}:normalize=0:dropout_transition=0,apad[voicebus]"
            )
        parts.append("[voicebus]asplit=2[voice][duckkey]")
        labels.append("[voice]")
    else:
        labels.extend(voice_labels)
    labels.extend(sfx_labels)

    if spec.bgm_path:
        if duck_bgm:
            parts.append(f"[{idx}:a]volume={spec.bgm_volume}[bgraw]")
            parts.append(
                f"[bgraw][duckkey]sidechaincompress="
                f"threshold={spec.bgm_duck_threshold}:ratio={spec.bgm_duck_ratio}:"
                f"attack={spec.bgm_duck_attack_ms}:release={spec.bgm_duck_release_ms}[bg]"
            )
        else:
            parts.append(f"[{idx}:a]volume={spec.bgm_volume}[bg]")
        labels.append("[bg]")
        idx += 1
    if spec.mix_with_original:
        labels = ["[0:a]"] + labels

    n = len(labels)
    mix_out = "[premix]" if spec.loudnorm else "[mix]"
    tail = "" if spec.loudnorm else ",apad"
    parts.append(
        "".join(labels)
        + f"amix=inputs={n}:normalize=0:dropout_transition=0{tail}{mix_out}"
    )
    if spec.loudnorm:
        parts.append(
            f"[premix]loudnorm=I={spec.loudnorm_i}:TP={spec.loudnorm_tp}:"
            f"LRA={spec.loudnorm_lra},apad[mix]"
        )
    return ";".join(parts)


def mux_audio(
    video_path: str,
    spec: AudioMixSpec,
    output_path: str,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ffmpeg: Optional[str] = None,
    duration_provider: Optional[Callable[[str], float]] = None,
) -> Optional[str]:
    """Assemble voiceover + sfx + BGM (+ loudnorm) in a single ffmpeg pass and
    mux the result onto the video. Video stream is copied (no re-encode).
    Returns the output path, or ``None`` on any failure / nothing-to-add so the
    caller keeps the original video.
    """
    exe = ffmpeg or _ffmpeg_exe()
    if exe is None:
        logger.warning("ffmpeg not found; skipping audio post for %s", video_path)
        return None
    if not spec.has_added_audio:
        return None

    target_duration = 0.0
    try:
        if duration_provider is None:
            from subtitles.timeline import probe_duration

            duration_provider = probe_duration
        target_duration = max(0.0, float(duration_provider(video_path)))
    except Exception:
        logger.debug("could not probe audio mix target duration for %s", video_path)

    cmd: List[str] = [exe, "-y", "-i", os.path.abspath(video_path)]
    for path, loop in build_audio_inputs(spec):
        if loop:
            cmd += ["-stream_loop", "-1"]
        cmd += ["-i", os.path.abspath(path)]
    cmd += [
        "-filter_complex", build_audio_filter(spec),
        "-map", "0:v", "-map", "[mix]",
        "-c:v", "copy", "-c:a", "aac", "-shortest",
    ]
    if target_duration > 0:
        # A looped BGM plus an apadded sidechain key is intentionally infinite.
        # Bound the output explicitly instead of relying on -shortest to drain it.
        cmd += ["-t", f"{target_duration:.3f}"]
    cmd.append(os.path.abspath(output_path))

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = runner(cmd, capture_output=True, text=True)
    except Exception as exc:  # pragma: no cover - environment dependent
        logger.warning("ffmpeg audio mux failed to launch: %s", exc)
        return None
    if proc.returncode != 0 or not os.path.exists(output_path):
        logger.warning("ffmpeg audio mux failed (code %s): %s", proc.returncode, (proc.stderr or "")[-500:])
        return None
    return output_path


def mux_voiceover(
    video_path: str,
    clips: Sequence[VoiceClip],
    output_path: str,
    mix_with_original: bool = False,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ffmpeg: Optional[str] = None,
) -> Optional[str]:
    """Place each voiceover clip at its start time and mux the result onto the
    video. Replaces the video's audio with the voiceover by default (set
    ``mix_with_original=True`` to keep the model-generated audio underneath).

    The video stream is stream-copied (``-c:v copy``) so there is no re-encode or
    quality loss. Returns the output path, or ``None`` on any failure so the
    caller falls back to the un-voiced video (never breaks a render).
    """
    exe = ffmpeg or _ffmpeg_exe()
    if exe is None:
        logger.warning("ffmpeg not found; skipping voiceover for %s", video_path)
        return None
    if not clips:
        return None

    cmd: List[str] = [exe, "-y", "-i", os.path.abspath(video_path)]
    for clip in clips:
        cmd += ["-i", os.path.abspath(clip.path)]
    cmd += [
        "-filter_complex", build_voiceover_filter(clips, mix_with_original),
        "-map", "0:v", "-map", "[mix]",
        "-c:v", "copy", "-c:a", "aac", "-shortest",
        os.path.abspath(output_path),
    ]

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = runner(cmd, capture_output=True, text=True)
    except Exception as exc:  # pragma: no cover - environment dependent
        logger.warning("ffmpeg voiceover mux failed to launch: %s", exc)
        return None
    if proc.returncode != 0 or not os.path.exists(output_path):
        stderr = (proc.stderr or "")[-500:]
        logger.warning("ffmpeg voiceover mux failed (code %s): %s", proc.returncode, stderr)
        return None
    return output_path
