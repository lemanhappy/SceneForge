from __future__ import annotations

import hashlib
import json
import math
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from subtitles import parse_srt, render_srt
from subtitles.models import SubtitleLine, SubtitleTrack
from utils.atomic import atomic_write_text


SCHEMA_VERSION = 1


class SubtitleTimelineService:
    """Aggregate, validate and persist the editable project subtitle sidecar."""

    def __init__(
        self,
        working_dir: str | Path,
        *,
        duration_provider: Callable[[str], float] | None = None,
    ) -> None:
        self.working_dir = Path(working_dir).resolve()
        self.idea_dir = self.working_dir / "idea2video"
        self.final_path = self.idea_dir / "final_video.mp4"
        self.subtitle_dir = self.idea_dir / "subtitles"
        self.plan_path = self.subtitle_dir / "timeline.json"
        self.srt_path = self.subtitle_dir / "final.srt"
        if duration_provider is None:
            from subtitles.timeline import probe_duration

            duration_provider = probe_duration
        self.duration_provider = duration_provider

    def get_plan(self) -> dict[str, Any]:
        default = self._default_plan()
        stored = self._read_json(self.plan_path, {})
        if not isinstance(stored, dict) or not isinstance(stored.get("lines"), list):
            return default
        if str(stored.get("source_fingerprint") or "") != default["source_fingerprint"]:
            return {**default, "stale_saved_timeline": True}
        try:
            normalized = self.validate(stored, default=default)
        except ValueError:
            normalized = {**default, "stale_saved_timeline": True}
        return normalized

    def save_plan(self, plan: Any) -> dict[str, Any]:
        normalized = self.validate(plan)
        self._archive_current("save")
        self.subtitle_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self.plan_path,
            json.dumps(normalized, ensure_ascii=False, indent=2),
        )
        self._write_srt(normalized)
        return normalized

    def reset(self) -> dict[str, Any]:
        self._archive_current("reset")
        self.plan_path.unlink(missing_ok=True)
        plan = self._default_plan()
        self._write_srt(plan)
        return plan

    def download_path(self) -> Path:
        plan = self.get_plan()
        if not plan["lines"]:
            raise ValueError("the project does not have subtitle lines")
        self._write_srt(plan)
        return self.srt_path

    def validate(
        self, plan: Any, *, default: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not isinstance(plan, dict):
            raise ValueError("subtitle timeline must be an object")
        default = default or self._default_plan()
        submitted = plan.get("lines")
        if not isinstance(submitted, list):
            raise ValueError("subtitle timeline lines must be a list")
        canonical = {line["line_id"]: line for line in default["lines"]}
        line_ids = [
            str(line.get("line_id") or "")
            for line in submitted
            if isinstance(line, dict)
        ]
        if len(line_ids) != len(submitted) or len(set(line_ids)) != len(line_ids):
            raise ValueError("each subtitle line must appear exactly once")
        if set(line_ids) != set(canonical):
            raise ValueError("subtitle lines do not match the current project")

        duration = float(default["duration"])
        lines = []
        previous_start = -1.0
        for order, raw in enumerate(submitted):
            base = canonical[str(raw["line_id"])]
            text = str(raw.get("text") or "").strip()
            if not text:
                raise ValueError("subtitle text cannot be empty")
            if len(text) > 500:
                raise ValueError("subtitle text cannot exceed 500 characters")
            try:
                start = round(float(raw.get("start")), 3)
                end = round(float(raw.get("end")), 3)
            except (TypeError, ValueError):
                raise ValueError("subtitle times must be numbers") from None
            if not math.isfinite(start) or not math.isfinite(end):
                raise ValueError("subtitle times must be finite numbers")
            if start < 0 or end > duration + 0.01:
                raise ValueError("subtitle times exceed the final video")
            if end - start < 0.1:
                raise ValueError("each subtitle line must last at least 0.1 seconds")
            if start < previous_start:
                raise ValueError("subtitle lines must stay in timeline order")
            previous_start = start
            lines.append({
                **base,
                "order": order,
                "text": text,
                "start": start,
                "end": end,
                "duration": round(end - start, 3),
            })
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "source_fingerprint": default["source_fingerprint"],
            "duration": default["duration"],
            "line_count": len(lines),
            "available": bool(lines),
            "stale_saved_timeline": False,
            "lines": lines,
        }

    def _default_plan(self) -> dict[str, Any]:
        if not self.final_path.is_file():
            raise ValueError("final video is not available")
        scenes = self._scene_tracks()
        final_duration = self._duration(self.final_path, 0.0)
        if final_duration <= 0:
            final_duration = sum(scene["duration"] for scene in scenes)
        applied = self._active_edit_plan()
        source_duration = (
            float(applied.get("source_duration") or final_duration)
            if applied is not None
            else final_duration
        )
        overlap = 0.0
        if len(scenes) > 1:
            overlap = max(
                0.0,
                (sum(scene["duration"] for scene in scenes) - source_duration)
                / (len(scenes) - 1),
            )
        cursor = 0.0
        lines = []
        for scene in scenes:
            for position, line in enumerate(scene["lines"]):
                start = max(0.0, cursor + float(line.get("start") or 0.0))
                end = min(source_duration, cursor + float(line.get("end") or 0.0))
                text = str(line.get("text") or "").strip()
                if end - start < 0.1 or not text:
                    continue
                lines.append({
                    "line_id": f"scene_{scene['scene_index']}_line_{position}",
                    "scene_index": scene["scene_index"],
                    "shot_idx": line.get("shot_idx"),
                    "speaker": line.get("speaker"),
                    "order": len(lines),
                    "text": text,
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "duration": round(end - start, 3),
                })
            cursor += max(0.0, scene["duration"] - overlap)
        lines = self._apply_timeline_edit(lines, applied, final_duration)
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at": None,
            "source_fingerprint": self._source_fingerprint(),
            "duration": round(final_duration, 3),
            "line_count": len(lines),
            "available": bool(lines),
            "stale_saved_timeline": False,
            "lines": lines,
        }

    def _scene_tracks(self) -> list[dict[str, Any]]:
        scenes = []
        for scene_dir in sorted(self.idea_dir.glob("scene_*"), key=self._scene_index):
            scene_index = self._scene_index(scene_dir)
            source = scene_dir / "audio" / "voiced_track.json"
            payload = self._read_json(source, {})
            raw_lines = payload.get("lines") if isinstance(payload, dict) else None
            if not isinstance(raw_lines, list) or not raw_lines:
                source = scene_dir / "subtitles" / "final.srt"
                try:
                    raw_lines = [line.model_dump() for line in parse_srt(str(source)).lines]
                except OSError:
                    raw_lines = []
            video = next((path for path in (
                scene_dir / "final_video_with_subtitles.mp4",
                scene_dir / "final_video_audio.mp4",
                scene_dir / "final_video.mp4",
            ) if path.is_file()), None)
            fallback = max(
                [float(line.get("end") or 0.0) for line in raw_lines if isinstance(line, dict)],
                default=0.0,
            )
            scenes.append({
                "scene_index": scene_index,
                "duration": self._duration(video, fallback) if video else fallback,
                "source": source,
                "lines": [line for line in raw_lines if isinstance(line, dict)],
            })
        return scenes

    def _apply_timeline_edit(
        self,
        lines: list[dict[str, Any]],
        applied: dict[str, Any] | None,
        final_duration: float,
    ) -> list[dict[str, Any]]:
        if applied is None:
            return lines
        transition = applied.get("transition") or {}
        crossfade = (
            float(transition.get("duration") or 0.0)
            if transition.get("type") == "crossfade"
            else 0.0
        )
        output = []
        cursor = 0.0
        for clip_index, clip in enumerate(applied.get("clips") or []):
            if clip_index:
                cursor -= crossfade
            clip_start = float(clip.get("source_start") or 0.0) + float(clip.get("trim_start") or 0.0)
            clip_end = float(clip.get("source_start") or 0.0) + float(clip.get("trim_end") or 0.0)
            for line in lines:
                overlap_start = max(line["start"], clip_start)
                overlap_end = min(line["end"], clip_end)
                if overlap_end - overlap_start < 0.1:
                    continue
                start = cursor + overlap_start - clip_start
                end = cursor + overlap_end - clip_start
                mapped_start = max(0.0, start)
                mapped_end = min(final_duration, max(0.0, end))
                if mapped_end - mapped_start < 0.1:
                    continue
                output.append({
                    **line,
                    "line_id": f"{line['line_id']}_clip_{clip.get('clip_id') or clip_index}",
                    "order": len(output),
                    "start": round(mapped_start, 3),
                    "end": round(mapped_end, 3),
                    "duration": round(mapped_end - mapped_start, 3),
                })
            cursor += max(0.0, clip_end - clip_start)
        output.sort(key=lambda line: (line["start"], line["end"], line["line_id"]))
        for order, line in enumerate(output):
            line["order"] = order
        return output

    def _active_edit_plan(self) -> dict[str, Any] | None:
        applied = self._read_json(
            self.idea_dir / "_editing" / "applied_edit_plan.json", {}
        )
        metadata = self._read_json(self.idea_dir / "_editing" / "source.json", {})
        if not isinstance(applied, dict) or not applied.get("clips"):
            return None
        if str(metadata.get("last_output_fingerprint") or "") != self._file_fingerprint(self.final_path):
            return None
        return applied

    def _write_srt(self, plan: dict[str, Any]) -> None:
        self.subtitle_dir.mkdir(parents=True, exist_ok=True)
        track = SubtitleTrack(lines=[
            SubtitleLine(
                text=line["text"],
                speaker=line.get("speaker"),
                shot_idx=line.get("shot_idx"),
                start=line["start"],
                end=line["end"],
            )
            for line in plan.get("lines") or []
        ])
        render_srt(track, str(self.srt_path))

    def _archive_current(self, action: str) -> Path | None:
        existing = [path for path in (self.plan_path, self.srt_path) if path.is_file()]
        if not existing:
            return None
        root = self.idea_dir / "_archive" / "subtitle_timelines"
        versions = []
        for candidate in root.glob("v*_*"):
            try:
                versions.append(int(candidate.name.split("_", 1)[0][1:]))
            except (IndexError, ValueError):
                continue
        archive = root / f"v{max(versions, default=0) + 1}_{action}"
        archive.mkdir(parents=True, exist_ok=False)
        for path in existing:
            shutil.copy2(path, archive / path.name)
        return archive

    def _source_fingerprint(self) -> str:
        digest = hashlib.sha256()
        sources = [self.final_path]
        for scene_dir in sorted(self.idea_dir.glob("scene_*"), key=self._scene_index):
            sources.extend([
                scene_dir / "audio" / "voiced_track.json",
                scene_dir / "subtitles" / "final.srt",
            ])
        for path in sources:
            if not path.is_file():
                continue
            stat = path.stat()
            digest.update(str(path.relative_to(self.idea_dir)).encode("utf-8"))
            digest.update(f":{stat.st_size}:{stat.st_mtime_ns}".encode("ascii"))
        return digest.hexdigest()

    def _duration(self, path: Path | None, fallback: float) -> float:
        if path is None:
            return max(0.0, float(fallback))
        try:
            value = float(self.duration_provider(str(path)))
        except Exception:
            value = float(fallback)
        return value if math.isfinite(value) and value > 0 else max(0.0, float(fallback))

    @staticmethod
    def _scene_index(path: Path) -> int:
        try:
            return int(path.name.split("_", 1)[1])
        except (IndexError, ValueError):
            return 0

    @staticmethod
    def _file_fingerprint(path: Path) -> str:
        if not path.is_file():
            return ""
        stat = path.stat()
        return f"{stat.st_size}:{stat.st_mtime_ns}"

    @staticmethod
    def _read_json(path: Path, default: Any) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default
