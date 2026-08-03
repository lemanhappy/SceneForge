import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .models import SubtitleStyle, SubtitleTrack

# ASS uses &HAABBGGRR. Map a few common names; unknown names fall back to white.
_ASS_COLORS = {
    "white": "&H00FFFFFF",
    "black": "&H00000000",
    "yellow": "&H0000FFFF",
    "red": "&H000000FF",
    "green": "&H0000FF00",
    "blue": "&H00FF0000",
}
_ALIGNMENT = {"bottom": 2, "middle": 5, "top": 8}
# Corner alignments for a persistent label/watermark (ASS numpad layout).
_CORNER_ALIGNMENT = {"bottom_left": 1, "bottom_center": 2, "bottom_right": 3,
                     "top_left": 7, "top_center": 8, "top_right": 9}


def _ass_color(name: str) -> str:
    """Resolve a colour name OR a ``#RRGGBB`` hex string to an ASS ``&HAABBGGRR``
    value (opaque). Unknown values fall back to white."""
    s = str(name).strip()
    if s.startswith("#") and len(s) == 7:
        try:
            rr, gg, bb = s[1:3], s[3:5], s[5:7]
            int(rr + gg + bb, 16)  # validate hex
            return f"&H00{bb}{gg}{rr}".upper()
        except ValueError:
            pass
    return _ASS_COLORS.get(s.lower(), "&H00FFFFFF")


def _ass_timestamp(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int(round((seconds - int(seconds)) * 100))
    if cs == 100:  # rounding spilled over
        cs = 0
        s += 1
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _srt_timestamp(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms == 1000:
        ms = 0
        s += 1
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _font_name(style: SubtitleStyle) -> str:
    if style.font_path:
        return Path(style.font_path).stem
    return style.fallback_fonts[0] if style.fallback_fonts else "Sans"


# Inline alignment override (\anN) per screen-text event so one style serves all
# positions. ASS numpad layout: 8=top-center, 5=middle-center, 2=bottom-center.
_SCREEN_AN = {"top": 8, "center": 5, "middle": 5, "bottom": 2}


def render_ass(track: SubtitleTrack, path: str, style: Optional[SubtitleStyle] = None,
               play_res_x: int = 1600, play_res_y: int = 900, hook: Optional[dict] = None,
               label: Optional[dict] = None, screen_texts: Optional[list] = None) -> str:
    style = style or SubtitleStyle()
    alignment = _ALIGNMENT.get(style.position, 2)
    style_lines = [
        f"Style: Default,{_font_name(style)},{style.font_size},{_ass_color(style.primary_color)},"
        f"{_ass_color(style.outline_color)},&H00000000,0,0,0,0,100,100,0,0,1,"
        f"{style.outline_width},0,{alignment},20,20,{style.margin_v},1"
    ]
    # Optional opening hook overlay (big, centred near the top by default) burned
    # in the same pass as the subtitles — no extra re-encode.
    hook_event = None
    if hook and (hook.get("text") or "").strip():
        h_align = _ALIGNMENT.get(hook.get("position", "top"), 8)
        h_size = int(hook.get("font_size", max(style.font_size + 20, 64)))
        h_color = _ass_color(hook.get("color", "#FFD24A"))
        h_outline = int(hook.get("outline_width", 3))
        h_margin = int(hook.get("margin_v", 60))
        seconds = float(hook.get("seconds", 3.0))
        style_lines.append(
            f"Style: Hook,{_font_name(style)},{h_size},{h_color},"
            f"{_ass_color(style.outline_color)},&H00000000,1,0,0,0,100,100,0,0,1,"
            f"{h_outline},0,{h_align},40,40,{h_margin},1"
        )
        hook_text = str(hook["text"]).strip().replace("\n", "\\N")
        hook_event = f"Dialogue: 1,{_ass_timestamp(0.0)},{_ass_timestamp(seconds)},Hook,,0,0,0,,{hook_text}"

    # Optional persistent label/watermark (e.g. AIGC compliance mark) shown in a
    # corner for the whole video — a single long event so no duration probe is
    # needed; ASS simply stops it at video end.
    label_event = None
    if label and (label.get("text") or "").strip():
        l_align = _CORNER_ALIGNMENT.get(label.get("position", "bottom_right"), 3)
        l_size = int(label.get("font_size", 30))
        l_color = _ass_color(label.get("color", "#FFFFFF"))
        l_margin = int(label.get("margin", 24))
        style_lines.append(
            f"Style: Label,{_font_name(style)},{l_size},{l_color},"
            f"&H64000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,{l_align},{l_margin},{l_margin},{l_margin},1"
        )
        label_text = str(label["text"]).strip().replace("\n", " ")
        label_event = f"Dialogue: 2,{_ass_timestamp(0.0)},{_ass_timestamp(359999.0)},Label,,0,0,0,,{label_text}"

    # Optional on-screen ("diegetic") text overlays — essential text (phone
    # notification, sign, balance) composited cleanly in post since image models
    # garble baked-in text. One boxed style for legibility; each event overrides
    # its own position with an inline \anN tag.
    screen_events = []
    screen_items = [s for s in (screen_texts or []) if (s.get("text") or "").strip()]
    if screen_items:
        s_size = int(round(style.font_size * 1.1))
        style_lines.append(
            f"Style: Screen,{_font_name(style)},{s_size},&H00FFFFFF,"
            f"&H00FFFFFF,&H78000000,1,0,0,0,100,100,0,0,3,3,0,5,40,40,40,1"
        )
        for s in screen_items:
            an = _SCREEN_AN.get(str(s.get("position") or "center"), 5)
            text = str(s["text"]).strip().replace("\n", "\\N")
            screen_events.append(
                f"Dialogue: 3,{_ass_timestamp(float(s.get('start', 0.0)))},"
                f"{_ass_timestamp(float(s.get('end', 0.0)))},Screen,,0,0,0,,"
                f"{{\\an{an}}}{text}"
            )

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {play_res_x}\n"
        f"PlayResY: {play_res_y}\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
        "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        + "\n".join(style_lines) + "\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    events = []
    if label_event:
        events.append(label_event)
    if hook_event:
        events.append(hook_event)
    events.extend(screen_events)
    for line in track.lines:
        text = line.text.replace("\n", "\\N")
        events.append(
            f"Dialogue: 0,{_ass_timestamp(line.start)},{_ass_timestamp(line.end)},Default,,0,0,0,,{text}"
        )
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return path


def render_srt(track: SubtitleTrack, path: str) -> str:
    blocks = []
    for i, line in enumerate(track.lines, start=1):
        blocks.append(f"{i}\n{_srt_timestamp(line.start)} --> {_srt_timestamp(line.end)}\n{line.text}\n")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(blocks) + "\n", encoding="utf-8")
    return path


def _ffmpeg_exe() -> Optional[str]:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:  # moviepy ships imageio-ffmpeg
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def burn_in(video_path: str, subtitle_path: str, output_path: str,
            style: Optional[SubtitleStyle] = None, metadata: Optional[dict] = None) -> Optional[str]:
    """Burn an .ass subtitle into the video with ffmpeg.

    Returns the output path on success, or ``None`` on failure (missing ffmpeg,
    bad font, etc.) so the caller can fall back to the subtitle-less video
    instead of crashing the whole production (design §15).

    The ffmpeg ``ass`` filter parses ``:`` as an option separator, which breaks
    on Windows drive-letter paths (``D:/...``). Rather than fight filtergraph
    escaping, run ffmpeg with cwd set to the subtitle's directory and reference
    it by basename, so the filter value contains no path separators or colons.
    """
    exe = _ffmpeg_exe()
    if exe is None:
        logging.warning("ffmpeg not found; skipping subtitle burn-in for %s", video_path)
        return None

    sub = Path(subtitle_path)
    sub_dir = str(sub.parent) or "."
    vf = f"ass={sub.name}"

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    cmd = [exe, "-y", "-i", os.path.abspath(video_path), "-vf", vf, "-c:a", "copy"]
    for key, value in (metadata or {}).items():
        cmd += ["-metadata", f"{key}={value}"]
    cmd += [os.path.abspath(output_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=sub_dir)
    except Exception as exc:  # pragma: no cover - environment dependent
        logging.warning("ffmpeg subtitle burn-in failed to launch: %s", exc)
        return None
    if proc.returncode != 0 or not os.path.exists(output_path):
        logging.warning("ffmpeg subtitle burn-in failed (code %s): %s", proc.returncode, proc.stderr[-500:])
        return None
    return output_path
