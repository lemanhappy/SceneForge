import logging
import requests
from moviepy import VideoFileClip, concatenate_videoclips
from utils.retry import download_retry


def normalize_transition(transition):
    """Normalize a transition spec (str like ``"crossfade"`` or a dict
    ``{"type": ..., "duration": ...}``) to ``{"type", "duration"}`` or ``None``.

    ``None``/``"none"``/non-positive duration all mean "no transition" so the
    default render stays a plain hard-cut concatenation (byte-identical to before
    this feature existed).
    """
    if not transition:
        return None
    if isinstance(transition, str):
        transition = {"type": transition}
    ttype = (transition.get("type") or "none").strip().lower()
    if ttype in ("", "none"):
        return None
    duration = float(transition.get("duration", 0.5))
    if duration <= 0:
        return None
    return {"type": ttype, "duration": duration}


def _concat_with_transition(clips, spec):
    """Concatenate clips with a transition between them. ``crossfade``/``dissolve``
    overlaps neighbours with a fade; ``fade`` dips each clip through black."""
    from moviepy import vfx

    duration = spec["duration"]
    ttype = spec["type"]
    if ttype in ("crossfade", "dissolve"):
        prepared = [clips[0]] + [c.with_effects([vfx.CrossFadeIn(duration)]) for c in clips[1:]]
        return concatenate_videoclips(prepared, method="compose", padding=-duration)
    if ttype == "fade":
        prepared = [c.with_effects([vfx.FadeIn(duration), vfx.FadeOut(duration)]) for c in clips]
        return concatenate_videoclips(prepared, method="compose")
    # Unknown type -> hard cut (defensive; normalize_transition gates known types).
    return concatenate_videoclips(clips)


@download_retry
def download_video(url, save_path):
    try:
        logging.info(f"Downloading video from {url} to {save_path}")

        response = requests.get(url, stream=True, timeout=(10, 300))
        response.raise_for_status()  # 检查请求是否成功
    
        with open(save_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

        logging.info(f"Video downloaded successfully to {save_path}")
    
    except Exception as e:
        logging.error(f"Error downloading video: {e}")
        raise e


def concatenate_video_files(video_paths, output_path, codec="libx264", preset="medium", transition=None):
    """Concatenate video files, releasing every ffmpeg reader even on failure.

    Each VideoFileClip keeps an ffmpeg subprocess and file handle open until
    closed; leaking them exhausts file descriptors on long multi-scene runs.

    ``transition`` (e.g. ``{"type": "crossfade", "duration": 0.5}``) adds a
    transition between clips; when omitted the result is a plain hard-cut
    concatenation, unchanged from the original behaviour.
    """
    spec = normalize_transition(transition)
    clips = []
    final = None
    try:
        for path in video_paths:
            clips.append(VideoFileClip(path))
        if spec is not None and len(clips) > 1:
            final = _concat_with_transition(clips, spec)
        else:
            final = concatenate_videoclips(clips)
        final.write_videofile(output_path, codec=codec, preset=preset)
    finally:
        if final is not None:
            final.close()
        for clip in clips:
            clip.close()
    return output_path


def render_timeline(
    source_path,
    ranges,
    output_path,
    *,
    transition=None,
    codec="libx264",
    preset="medium",
):
    """Render ordered subclips from one immutable source video."""
    spec = normalize_transition(transition)
    source = VideoFileClip(source_path)
    clips = []
    final = None
    try:
        for item in ranges:
            start = max(0.0, float(item["start"]))
            end = min(float(source.duration), float(item["end"]))
            if end - start < 0.1:
                raise ValueError("timeline clip must keep at least 0.1 seconds")
            clips.append(source.subclipped(start, end))
        if not clips:
            raise ValueError("timeline must contain at least one clip")
        if spec is not None and len(clips) > 1:
            final = _concat_with_transition(clips, spec)
        else:
            final = concatenate_videoclips(clips, method="compose")
        final.write_videofile(
            output_path,
            codec=codec,
            audio_codec="aac",
            preset=preset,
        )
    finally:
        if final is not None:
            final.close()
        for clip in clips:
            clip.close()
        source.close()
    return output_path
