import logging
import os
import subprocess
import tempfile
from pathlib import Path

import requests

from utils.media import ffmpeg_executable, media_has_audio, probe_media_duration
from utils.retry import download_retry


def normalize_transition(transition):
    """Normalize a transition spec to ``{"type", "duration"}`` or ``None``."""
    if not transition:
        return None
    if isinstance(transition, str):
        transition = {"type": transition}
    transition_type = (transition.get("type") or "none").strip().lower()
    if transition_type in ("", "none"):
        return None
    duration = float(transition.get("duration", 0.5))
    if duration <= 0:
        return None
    return {"type": transition_type, "duration": duration}


@download_retry
def download_video(url, save_path):
    try:
        logging.info("Downloading video from %s to %s", url, save_path)
        response = requests.get(url, stream=True, timeout=(10, 300))
        response.raise_for_status()
        with open(save_path, "wb") as output:
            for chunk in response.iter_content(chunk_size=8192):
                output.write(chunk)
        logging.info("Video downloaded successfully to %s", save_path)
    except Exception:
        logging.exception("Video download failed: %s", url)
        raise


def _run_ffmpeg(command):
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown FFmpeg error").strip()
        raise RuntimeError(f"FFmpeg render failed: {detail[-2000:]}")


def _ffmpeg_or_raise():
    executable = ffmpeg_executable()
    if executable:
        return executable
    raise RuntimeError("FFmpeg is required for video editing")


def _concat_file_line(path):
    escaped = Path(path).resolve().as_posix().replace("'", "'\\''")
    return f"file '{escaped}'\n"


def _transition_overlaps(durations, requested):
    overlaps = []
    last = len(durations) - 1
    for index in range(last):
        left_divisor = 3 if 0 < index < last else 2
        right_index = index + 1
        right_divisor = 3 if 0 < right_index < last else 2
        overlaps.append(min(
            requested,
            max(0.01, durations[index] / left_divisor),
            max(0.01, durations[right_index] / right_divisor),
        ))
    return overlaps


def _crossfade_filter(input_labels, durations, requested, *, prefix):
    overlaps = _transition_overlaps(durations, requested)
    parts = []
    stable_labels = []
    head_labels = {}
    tail_labels = {}
    for index, (label, clip_duration) in enumerate(zip(input_labels, durations)):
        sources = [f"[{prefix}s{index}]"]
        if index > 0:
            sources.append(f"[{prefix}hsrc{index}]")
        if index < len(input_labels) - 1:
            sources.append(f"[{prefix}tsrc{index}]")
        parts.append(f"{label}split={len(sources)}{''.join(sources)}")

        start = overlaps[index - 1] if index > 0 else 0.0
        end = clip_duration - (overlaps[index] if index < len(overlaps) else 0.0)
        stable = f"[{prefix}stable{index}]"
        parts.append(
            f"{sources[0]}trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS{stable}"
        )
        stable_labels.append(stable)
        source_index = 1
        if index > 0:
            head = f"[{prefix}head{index}]"
            parts.append(
                f"{sources[source_index]}trim=start=0:end={overlaps[index - 1]:.3f},"
                f"setpts=PTS-STARTPTS{head}"
            )
            head_labels[index] = head
            source_index += 1
        if index < len(input_labels) - 1:
            tail = f"[{prefix}tail{index}]"
            parts.append(
                f"{sources[source_index]}trim=start={clip_duration - overlaps[index]:.3f}:"
                f"end={clip_duration:.3f},setpts=PTS-STARTPTS{tail}"
            )
            tail_labels[index] = tail

    ordered = []
    for index, stable in enumerate(stable_labels):
        ordered.append(stable)
        if index < len(overlaps):
            blended = f"[{prefix}blend{index}]"
            overlap = overlaps[index]
            parts.append(
                f"{tail_labels[index]}{head_labels[index + 1]}"
                f"blend=all_expr='A*(1-T/{overlap:.3f})+B*(T/{overlap:.3f})'{blended}"
            )
            ordered.append(blended)
    output = f"[{prefix}out]"
    parts.append("".join(ordered) + f"concat=n={len(ordered)}:v=1:a=0{output}")
    return parts, output


def _transition_filter(input_labels, durations, spec, *, prefix="v"):
    duration = spec["duration"]
    transition_type = spec["type"]
    if transition_type in ("crossfade", "dissolve"):
        return _crossfade_filter(input_labels, durations, duration, prefix=prefix)
    if transition_type == "fade":
        parts = []
        faded = []
        for index, (label, clip_duration) in enumerate(zip(input_labels, durations)):
            fade_duration = min(duration, max(0.01, clip_duration / 2))
            output = f"[{prefix}fade{index}]"
            parts.append(
                f"{label}fade=t=in:st=0:d={fade_duration:.3f},"
                f"fade=t=out:st={max(0, clip_duration - fade_duration):.3f}:d={fade_duration:.3f}{output}"
            )
            faded.append(output)
        output = f"[{prefix}out]"
        parts.append("".join(faded) + f"concat=n={len(faded)}:v=1:a=0{output}")
        return parts, output
    output = f"[{prefix}out]"
    return ["".join(input_labels) + f"concat=n={len(input_labels)}:v=1:a=0{output}"], output


def concatenate_video_files(video_paths, output_path, codec="libx264", preset="medium", transition=None):
    """Concatenate generated clips with FFmpeg while preserving available audio."""
    if not video_paths:
        raise ValueError("at least one video is required")
    executable = _ffmpeg_or_raise()
    spec = normalize_transition(transition)
    if len(video_paths) < 2:
        spec = None
    output = str(Path(output_path).resolve())
    Path(output).parent.mkdir(parents=True, exist_ok=True)

    if spec is None:
        list_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".txt",
                prefix="sceneforge-concat-",
                dir=Path(output).parent,
                delete=False,
            ) as concat_file:
                list_path = concat_file.name
                for path in video_paths:
                    concat_file.write(_concat_file_line(path))
            command = [
                executable, "-y", "-f", "concat", "-safe", "0", "-i", list_path,
                "-c:v", codec, "-preset", preset, "-c:a", "aac", "-movflags", "+faststart", output,
            ]
            _run_ffmpeg(command)
        finally:
            if list_path:
                Path(list_path).unlink(missing_ok=True)
        return output_path

    durations = [probe_media_duration(path, ffmpeg=executable) for path in video_paths]
    command = [executable, "-y"]
    for path in video_paths:
        command.extend(["-i", str(Path(path).resolve())])
    video_labels = []
    filters = []
    for index in range(len(video_paths)):
        label = f"[vin{index}]"
        filters.append(f"[{index}:v]fps=30,format=yuv420p,setpts=PTS-STARTPTS{label}")
        video_labels.append(label)
    transition_filters, video_output = _transition_filter(video_labels, durations, spec)
    filters.extend(transition_filters)
    has_audio = all(media_has_audio(path, ffmpeg=executable) for path in video_paths)
    audio_output = None
    if has_audio:
        if spec["type"] in ("crossfade", "dissolve"):
            overlaps = _transition_overlaps(durations, spec["duration"])
            previous = "[0:a]"
            for index in range(1, len(video_paths)):
                overlap = overlaps[index - 1]
                audio_output = f"[a{index}]"
                filters.append(f"{previous}[{index}:a]acrossfade=d={overlap:.3f}{audio_output}")
                previous = audio_output
        else:
            audio_labels = []
            for index, clip_duration in enumerate(durations):
                fade_duration = min(spec["duration"], max(0.01, clip_duration / 2))
                label = f"[afade{index}]"
                filters.append(
                    f"[{index}:a]afade=t=in:st=0:d={fade_duration:.3f},"
                    f"afade=t=out:st={max(0, clip_duration - fade_duration):.3f}:d={fade_duration:.3f}{label}"
                )
                audio_labels.append(label)
            audio_output = "[aout]"
            filters.append("".join(audio_labels) + f"concat=n={len(audio_labels)}:v=0:a=1{audio_output}")
    command.extend(["-filter_complex", ";".join(filters), "-map", video_output])
    if audio_output:
        command.extend(["-map", audio_output, "-c:a", "aac"])
    command.extend(["-c:v", codec, "-preset", preset, "-movflags", "+faststart", output])
    _run_ffmpeg(command)
    return output_path


def render_timeline(source_path, ranges, output_path, *, transition=None, codec="libx264", preset="medium"):
    """Render ordered subclips from one immutable source video."""
    if not ranges:
        raise ValueError("timeline must contain at least one clip")
    executable = _ffmpeg_or_raise()
    source_duration = probe_media_duration(source_path, ffmpeg=executable)
    normalized = []
    for item in ranges:
        start = max(0.0, float(item["start"]))
        end = min(source_duration, float(item["end"]))
        if end - start < 0.1:
            raise ValueError("timeline clip must keep at least 0.1 seconds")
        normalized.append((start, end))

    filters = []
    video_labels = []
    audio_labels = []
    source_has_audio = media_has_audio(source_path, ffmpeg=executable)
    for index, (start, end) in enumerate(normalized):
        video_label = f"[trimv{index}]"
        filters.append(
            f"[0:v]trim=start={start:.3f}:end={end:.3f},"
            f"fps=30,format=yuv420p,setpts=PTS-STARTPTS{video_label}"
        )
        video_labels.append(video_label)
        if source_has_audio:
            audio_label = f"[trima{index}]"
            filters.append(f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS{audio_label}")
            audio_labels.append(audio_label)

    durations = [end - start for start, end in normalized]
    spec = normalize_transition(transition)
    if spec and len(video_labels) > 1:
        video_filters, video_output = _transition_filter(video_labels, durations, spec, prefix="tv")
        filters.extend(video_filters)
    else:
        video_output = "[vout]"
        filters.append("".join(video_labels) + f"concat=n={len(video_labels)}:v=1:a=0{video_output}")

    audio_output = None
    if audio_labels:
        audio_output = "[aout]"
        if spec and spec["type"] in ("crossfade", "dissolve") and len(audio_labels) > 1:
            overlaps = _transition_overlaps(durations, spec["duration"])
            previous = audio_labels[0]
            for index, label in enumerate(audio_labels[1:], start=1):
                overlap = overlaps[index - 1]
                output = audio_output if index == len(audio_labels) - 1 else f"[tax{index}]"
                filters.append(f"{previous}{label}acrossfade=d={overlap:.3f}{output}")
                previous = output
        else:
            filters.append("".join(audio_labels) + f"concat=n={len(audio_labels)}:v=0:a=1{audio_output}")
    Path(output_path).resolve().parent.mkdir(parents=True, exist_ok=True)
    command = [
        executable, "-y", "-i", str(Path(source_path).resolve()),
        "-filter_complex", ";".join(filters), "-map", video_output,
    ]
    if audio_output:
        command.extend(["-map", audio_output, "-c:a", "aac"])
    command.extend([
        "-c:v", codec, "-preset", preset, "-movflags", "+faststart", str(Path(output_path).resolve()),
    ])
    _run_ffmpeg(command)
    return output_path
