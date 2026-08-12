"""Imported-video post-production: take an external video and re-dub / subtitle /
score / label it, reusing the same building blocks the generator uses.

Capabilities (all on an arbitrary input video):
  - transcribe()  : ASR (gateway whisper) -> spoken segments with timestamps,
                    to populate an editable line list (e.g. for narration edits).
  - apply()       : burn subtitles + add/replace voiceover (TTS) + BGM + AIGC
                    label in one pass, via subtitles/render + audio/mux_audio.

What this does NOT do: lip-synced in-place dialogue changes (needs a lip-sync
model not in the stack). Replacing a *narration* line works because we replace
the audio track, not the mouth.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils.uploads import DEFAULT_VIDEO_UPLOAD_BYTES, decode_base64_upload, upload_limit

_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
_SAFE_RE = re.compile(r"[^0-9A-Za-z._\-一-鿿]+")


def _safe_name(filename: str) -> str:
    base = os.path.basename(str(filename or "").replace("\\", "/")).strip()
    stem, ext = os.path.splitext(base)
    stem = _SAFE_RE.sub("_", stem).strip("_") or "video"
    return stem + (ext.lower() if ext.lower() in _VIDEO_EXTS else ".mp4")


class VideoEditService:
    def __init__(self, imports_dir: str = "assets/imports", workspace_root: str = "."):
        self.imports_dir = Path(imports_dir)
        self.workspace_root = workspace_root

    # ----- library --------------------------------------------------------

    def list_videos(self) -> List[str]:
        if not self.imports_dir.is_dir():
            return []
        return sorted(p.name for p in self.imports_dir.iterdir()
                      if p.is_file() and p.suffix.lower() in _VIDEO_EXTS)

    def upload(self, filename: str, data_b64: str) -> dict:
        raw = decode_base64_upload(
            data_b64,
            max_bytes=upload_limit("SCENEFORGE_MAX_VIDEO_BYTES", DEFAULT_VIDEO_UPLOAD_BYTES),
        )
        self.imports_dir.mkdir(parents=True, exist_ok=True)
        name = _safe_name(filename)
        (self.imports_dir / name).write_bytes(raw)
        return {"name": name, "videos": self.list_videos()}

    def _resolve(self, path: str) -> str:
        if not path:
            raise ValueError("video path/name required")
        if os.path.isabs(path) and os.path.isfile(path):
            return path
        cand = self.imports_dir / _safe_name(path)
        if cand.is_file():
            return str(cand)
        # also accept a name passed verbatim (already safe)
        cand2 = self.imports_dir / os.path.basename(path)
        if cand2.is_file():
            return str(cand2)
        raise FileNotFoundError(f"video not found: {path}")

    # ----- ASR ------------------------------------------------------------

    async def transcribe(self, path: str, model: str = "whisper-1") -> dict:
        """Return {segments:[{start,end,text}]} via the gateway's whisper. Fails
        soft (returns an error field, empty segments) so the UI degrades."""
        from audio.mixer import _ffmpeg_exe
        from agent_runtime.config import tts_api_key, tts_base_url

        video = self._resolve(path)
        exe = _ffmpeg_exe()
        key = tts_api_key(self.workspace_root)
        if exe is None or not key:
            return {"segments": [], "error": "missing ffmpeg or api_key"}
        with tempfile.TemporaryDirectory() as d:
            audio = os.path.join(d, "audio.mp3")
            try:
                subprocess.run([exe, "-y", "-i", os.path.abspath(video), "-vn", "-ac", "1",
                                "-ar", "16000", audio], capture_output=True, text=True)
            except Exception as exc:  # pragma: no cover
                return {"segments": [], "error": f"audio extract failed: {exc}"}
            if not os.path.exists(audio):
                return {"segments": [], "error": "audio extract produced no file"}
            import requests
            base = tts_base_url(self.workspace_root).rstrip("/")
            try:
                with open(audio, "rb") as f:
                    r = requests.post(base + "/audio/transcriptions",
                                      headers={"Authorization": f"Bearer {key}"},
                                      files={"file": ("audio.mp3", f, "audio/mpeg")},
                                      data={"model": model, "response_format": "verbose_json"},
                                      timeout=300)
                r.raise_for_status()
                body = r.json()
            except Exception as exc:
                return {"segments": [], "error": f"transcription failed: {exc}"}
        segments = []
        for s in (body.get("segments") or []):
            segments.append({"start": round(float(s.get("start", 0)), 2),
                             "end": round(float(s.get("end", 0)), 2),
                             "text": str(s.get("text", "")).strip()})
        if not segments and body.get("text"):
            segments = [{"start": 0.0, "end": 0.0, "text": str(body["text"]).strip()}]
        return {"segments": segments}

    # ----- edit -----------------------------------------------------------

    async def apply(self, path: str, spec: dict) -> dict:
        """Apply post-production to an imported video. ``spec``:
          subtitles: [{text,start,end}]      -> burned subtitles
          voiceover: [{text,start}]          -> TTS, muxed (replace or overlay)
          bgm: {path, volume}                -> background music
          aigc_label: {text, position, ...}  -> persistent corner label
          voice: {provider, model, voice}    -> TTS engine (else config defaults)
          replace_audio: bool                -> replace original audio (default True if voiceover)
        Returns {output}. Reuses subtitles/render + audio/mux_audio + AIGC label.
        """
        from audio.models import AudioMixSpec, VoiceClip
        from audio.mixer import mux_audio
        from subtitles.models import SubtitleTrack, SubtitleLine
        from subtitles.renderer import render_ass, burn_in

        video = self._resolve(path)
        out_dir = self.imports_dir / "_edited"
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = Path(video).stem
        current = video

        # 1) audio: voiceover (+ bgm) muxed in one pass
        voiceover = spec.get("voiceover") or []
        bgm = spec.get("bgm") or {}
        if voiceover or bgm.get("path"):
            clips = await self._synth_voiceover(voiceover, spec.get("voice") or {}, out_dir / "_vo")
            audio_spec = AudioMixSpec(
                voiceover=clips,
                bgm_path=(bgm.get("path") if bgm.get("path") and os.path.exists(bgm.get("path")) else None),
                bgm_volume=float(bgm.get("volume", 0.2)),
                loudnorm=True,
                mix_with_original=not bool(spec.get("replace_audio", True)),
            )
            if audio_spec.has_added_audio:
                muxed = str(out_dir / f"{stem}_audio.mp4")
                result = mux_audio(current, audio_spec, muxed)
                if result:
                    current = result

        # 2) subtitles + AIGC label burned in one pass
        sub_lines = spec.get("subtitles") or []
        label = spec.get("aigc_label") or None
        if label and not (label.get("text") or "").strip():
            label = None
        if sub_lines or label:
            track = SubtitleTrack(lines=[SubtitleLine(text=str(l.get("text", "")),
                                                      start=float(l.get("start", 0)),
                                                      end=float(l.get("end", 0)))
                                         for l in sub_lines if str(l.get("text", "")).strip()])
            ass = render_ass(track, str(out_dir / f"{stem}.ass"), hook=None, label=label)
            burned = str(out_dir / f"{stem}_edited.mp4")
            metadata = {"comment": f"AIGC {label['text']}", "AIGC": "true"} if label else None
            res = burn_in(current, ass, burned, metadata=metadata)
            if res:
                current = res

        if current == video:
            return {"output": None, "note": "未指定任何编辑操作"}
        return {"output": current}

    async def _synth_voiceover(self, lines: List[dict], voice_cfg: dict, out_dir: Path) -> List[Any]:
        from audio.models import VoiceClip
        from audio.service import make_provider
        from agent_runtime.config import tts_api_key, tts_base_url
        clips = []
        lines = [l for l in lines if str(l.get("text", "")).strip()]
        if not lines:
            return clips
        key = tts_api_key(self.workspace_root)
        if not key:
            return clips
        provider = make_provider(key, tts_base_url(self.workspace_root), voice_cfg)
        out_dir.mkdir(parents=True, exist_ok=True)
        for i, line in enumerate(lines):
            dst = str(out_dir / f"vo_{i:03d}.mp3")
            rendered = provider.synthesize(str(line["text"]).strip(), dst)
            if rendered:
                clips.append(VoiceClip(path=rendered, start=float(line.get("start", 0)), text=str(line["text"])))
        return clips
