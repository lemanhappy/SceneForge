from __future__ import annotations

import json
import math
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from utils.atomic import atomic_write_text


SCHEMA_VERSION = 1
ALLOWED_TRANSITIONS = {"none", "crossfade", "fade"}


class TimelineEditService:
    """Persist and render a non-destructive edit plan for one generated project."""

    def __init__(
        self,
        working_dir: str | Path,
        *,
        duration_provider: Callable[[str], float] | None = None,
        renderer: Callable[..., str] | None = None,
    ) -> None:
        self.working_dir = Path(working_dir).resolve()
        self.idea_dir = self.working_dir / "idea2video"
        self.final_path = self.idea_dir / "final_video.mp4"
        self.plan_path = self.idea_dir / "edit_plan.json"
        self.editing_dir = self.idea_dir / "_editing"
        self.source_path = self.editing_dir / "source_video.mp4"
        self.metadata_path = self.editing_dir / "source.json"
        self.applied_plan_path = self.editing_dir / "applied_edit_plan.json"
        if duration_provider is None:
            from subtitles.timeline import probe_duration

            duration_provider = probe_duration
        if renderer is None:
            from utils.video import render_timeline

            renderer = render_timeline
        self.duration_provider = duration_provider
        self.renderer = renderer

    def get_plan(self) -> dict[str, Any]:
        state = self._source_state()
        default = self._default_plan(state)
        stored = self._read_json(self.plan_path, {})
        if not isinstance(stored, dict) or not stored.get("clips"):
            return default
        if str(stored.get("base_fingerprint") or "") != default["base_fingerprint"]:
            return {**default, "stale_saved_plan": True}
        try:
            return self.validate(stored, default=default)
        except ValueError:
            return {**default, "stale_saved_plan": True}

    def output_fingerprint(self) -> str:
        if not self.final_path.is_file():
            raise ValueError("final video is not available")
        return self._fingerprint(self.final_path)

    def has_original_source(self) -> bool:
        try:
            return self._source_state()["status"] == "ready"
        except ValueError:
            return False

    def save_plan(self, plan: Any) -> dict[str, Any]:
        normalized = self.validate(plan)
        self.idea_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self.plan_path,
            json.dumps(normalized, ensure_ascii=False, indent=2),
        )
        return normalized

    def validate(
        self, plan: Any, *, default: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not isinstance(plan, dict):
            raise ValueError("edit plan must be an object")
        default = default or self._default_plan(self._source_state())
        submitted = plan.get("clips")
        if not isinstance(submitted, list) or not submitted:
            raise ValueError("edit plan must contain clips")

        canonical = {item["clip_id"]: item for item in default["clips"]}
        clip_ids = [str(item.get("clip_id") or "") for item in submitted if isinstance(item, dict)]
        if len(clip_ids) != len(submitted) or len(set(clip_ids)) != len(clip_ids):
            raise ValueError("each clip must appear exactly once")
        if set(clip_ids) != set(canonical):
            raise ValueError("edit plan clips do not match the current project")

        clips = []
        for order, raw in enumerate(submitted):
            base = canonical[str(raw["clip_id"])]
            source_duration = float(base["source_duration"])
            try:
                trim_start = round(float(raw.get("trim_start", 0)), 3)
                trim_end = round(float(raw.get("trim_end", source_duration)), 3)
            except (TypeError, ValueError):
                raise ValueError("trim points must be numbers") from None
            if not math.isfinite(trim_start) or not math.isfinite(trim_end):
                raise ValueError("trim points must be finite numbers")
            if trim_start < 0 or trim_end > source_duration + 0.001:
                raise ValueError(f"trim points exceed source clip {base['clip_id']}")
            if trim_end - trim_start < 0.1:
                raise ValueError(f"clip {base['clip_id']} must keep at least 0.1 seconds")
            clips.append({
                **base,
                "order": order,
                "trim_start": trim_start,
                "trim_end": trim_end,
                "output_duration": round(trim_end - trim_start, 3),
            })

        raw_transition = plan.get("transition")
        raw_transition = raw_transition if isinstance(raw_transition, dict) else {}
        transition_type = str(raw_transition.get("type") or "none").strip().lower()
        if transition_type not in ALLOWED_TRANSITIONS:
            raise ValueError("transition type must be none, crossfade, or fade")
        try:
            transition_duration = round(float(raw_transition.get("duration", 0)), 3)
        except (TypeError, ValueError):
            raise ValueError("transition duration must be a number") from None
        if not math.isfinite(transition_duration):
            raise ValueError("transition duration must be a finite number")
        if transition_type == "none":
            transition_duration = 0.0
        elif transition_duration < 0.1 or transition_duration > 2.0:
            raise ValueError("transition duration must be between 0.1 and 2 seconds")
        if transition_type == "crossfade" and len(clips) > 1:
            max_duration = min(item["output_duration"] for item in clips) / 2
            if transition_duration > max_duration:
                raise ValueError("crossfade duration is too long for the shortest clip")

        output_duration = sum(item["output_duration"] for item in clips)
        if transition_type == "crossfade":
            output_duration -= transition_duration * max(0, len(clips) - 1)
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "base_fingerprint": default["base_fingerprint"],
            "source_status": default["source_status"],
            "source_duration": default["source_duration"],
            "output_duration": round(max(0.0, output_duration), 3),
            "transition": {
                "type": transition_type,
                "duration": transition_duration,
            },
            "clips": clips,
            "stale_saved_plan": False,
        }

    def render(self, plan: Any) -> dict[str, Any]:
        normalized = self.save_plan(plan)
        source = self._prepare_source(normalized)
        normalized = self.validate(normalized)
        self.save_plan(normalized)
        temporary = self.final_path.with_name(
            f".{self.final_path.name}.{uuid4().hex}.editing.mp4"
        )
        ranges = [
            {
                "clip_id": item["clip_id"],
                "start": round(item["source_start"] + item["trim_start"], 3),
                "end": round(item["source_start"] + item["trim_end"], 3),
            }
            for item in normalized["clips"]
        ]
        try:
            self.renderer(
                str(source),
                ranges,
                str(temporary),
                transition=normalized["transition"],
            )
            if not temporary.is_file() or temporary.stat().st_size <= 0:
                raise RuntimeError("timeline render did not produce a video")
            archive = self._archive_current("render")
            os.replace(temporary, self.final_path)
            atomic_write_text(
                self.applied_plan_path,
                json.dumps(normalized, ensure_ascii=False, indent=2),
            )
            metadata = self._read_json(self.metadata_path, {})
            metadata["last_output_fingerprint"] = self._fingerprint(self.final_path)
            metadata["last_rendered_at"] = datetime.now(timezone.utc).isoformat(
                timespec="milliseconds"
            )
            atomic_write_text(
                self.metadata_path,
                json.dumps(metadata, ensure_ascii=False, indent=2),
            )
            return {
                "ok": True,
                "final_video_path": str(self.final_path),
                "archive_path": str(archive),
                "output_duration": normalized["output_duration"],
                "clip_count": len(normalized["clips"]),
                "plan": normalized,
            }
        finally:
            temporary.unlink(missing_ok=True)

    def reset(self) -> dict[str, Any]:
        if not self.has_original_source():
            raise ValueError("the project does not have an original edit source")
        archive = self._archive_current("reset")
        temporary = self.final_path.with_name(
            f".{self.final_path.name}.{uuid4().hex}.reset.mp4"
        )
        shutil.copy2(self.source_path, temporary)
        os.replace(temporary, self.final_path)
        self.plan_path.unlink(missing_ok=True)
        self.applied_plan_path.unlink(missing_ok=True)
        metadata = self._read_json(self.metadata_path, {})
        metadata["last_output_fingerprint"] = self._fingerprint(self.final_path)
        atomic_write_text(
            self.metadata_path,
            json.dumps(metadata, ensure_ascii=False, indent=2),
        )
        plan = self.get_plan()
        return {
            "ok": True,
            "final_video_path": str(self.final_path),
            "archive_path": str(archive),
            "output_duration": plan["output_duration"],
            "clip_count": len(plan["clips"]),
            "plan": plan,
        }

    def _default_plan(self, state: dict[str, Any]) -> dict[str, Any]:
        source = state["path"]
        if not source.is_file():
            raise ValueError("final video is not available")
        shots = self._storyboard_shots()
        if not shots:
            raise ValueError("storyboard clips are not available")
        weights = []
        for item in shots:
            try:
                weight = float(item.get("duration_sec") or 5.0)
            except (TypeError, ValueError):
                weight = 5.0
            weights.append(max(0.1, weight) if math.isfinite(weight) else 5.0)
        fallback_duration = sum(weights)
        try:
            source_duration = float(state.get("duration") or self.duration_provider(str(source)))
        except Exception:
            source_duration = fallback_duration
        if not math.isfinite(source_duration) or source_duration <= 0:
            source_duration = fallback_duration
        scale = source_duration / fallback_duration if fallback_duration else 1.0
        cursor = 0.0
        clips = []
        for index, (shot, weight) in enumerate(zip(shots, weights)):
            end = source_duration if index == len(shots) - 1 else cursor + weight * scale
            duration = round(max(0.1, end - cursor), 3)
            clips.append({
                "clip_id": f"{shot['scene_index']}_{shot['shot_idx']}",
                "scene_index": shot["scene_index"],
                "shot_idx": shot["shot_idx"],
                "label": f"场景 {shot['scene_index'] + 1} · 镜头 {shot['shot_idx'] + 1}",
                "order": index,
                "source_start": round(cursor, 3),
                "source_end": round(end, 3),
                "source_duration": duration,
                "trim_start": 0.0,
                "trim_end": duration,
                "output_duration": duration,
            })
            cursor = end
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at": None,
            "base_fingerprint": state["base_fingerprint"],
            "source_status": state["status"],
            "source_duration": round(source_duration, 3),
            "output_duration": round(source_duration, 3),
            "transition": {"type": "none", "duration": 0.0},
            "clips": clips,
            "stale_saved_plan": False,
        }

    def _storyboard_shots(self) -> list[dict[str, Any]]:
        shots = []
        for scene_dir in sorted(self.idea_dir.glob("scene_*"), key=self._scene_index):
            storyboard = self._read_json(scene_dir / "storyboard.json", [])
            if not isinstance(storyboard, list):
                continue
            scene_index = self._scene_index(scene_dir)
            for position, item in enumerate(storyboard):
                item = item if isinstance(item, dict) else {}
                try:
                    shot_index = int(item.get("idx", position))
                except (TypeError, ValueError):
                    shot_index = position
                shots.append({
                    "scene_index": scene_index,
                    "shot_idx": shot_index,
                    "duration_sec": item.get("duration_sec", 5.0),
                })
        return shots

    def _source_state(self) -> dict[str, Any]:
        if not self.final_path.is_file():
            raise ValueError("final video is not available")
        live_fingerprint = self._fingerprint(self.final_path)
        metadata = self._read_json(self.metadata_path, {})
        source_fingerprint = str(metadata.get("source_fingerprint") or "")
        last_output = str(metadata.get("last_output_fingerprint") or "")
        if self.source_path.is_file() and live_fingerprint in {source_fingerprint, last_output}:
            return {
                "status": "ready",
                "path": self.source_path,
                "duration": metadata.get("source_duration"),
                "base_fingerprint": source_fingerprint,
            }
        return {
            "status": "refresh_required" if self.source_path.exists() else "will_initialize",
            "path": self.final_path,
            "duration": None,
            "base_fingerprint": live_fingerprint,
        }

    def _prepare_source(self, plan: dict[str, Any]) -> Path:
        state = self._source_state()
        if state["status"] == "ready":
            return self.source_path
        if self.editing_dir.exists():
            stale = self._next_archive_dir("stale_source")
            stale.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(self.editing_dir), str(stale))
        self.editing_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.final_path, self.source_path)
        source_fingerprint = self._fingerprint(self.source_path)
        metadata = {
            "schema_version": SCHEMA_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "source_fingerprint": source_fingerprint,
            "source_duration": plan["source_duration"],
            "last_output_fingerprint": self._fingerprint(self.final_path),
        }
        atomic_write_text(
            self.metadata_path,
            json.dumps(metadata, ensure_ascii=False, indent=2),
        )
        return self.source_path

    def _archive_current(self, action: str) -> Path:
        archive = self._next_archive_dir(action)
        archive.mkdir(parents=True, exist_ok=False)
        if self.final_path.is_file():
            shutil.copy2(self.final_path, archive / self.final_path.name)
        if self.applied_plan_path.is_file():
            shutil.copy2(self.applied_plan_path, archive / self.applied_plan_path.name)
        if self.plan_path.is_file():
            shutil.copy2(self.plan_path, archive / self.plan_path.name)
        return archive

    def _next_archive_dir(self, action: str) -> Path:
        root = self.idea_dir / "_archive" / "timeline_edits"
        versions = []
        for candidate in root.glob("v*_*"):
            try:
                versions.append(int(candidate.name.split("_", 1)[0][1:]))
            except (IndexError, ValueError):
                continue
        version = max(versions, default=0) + 1
        return root / f"v{version}_{action}"

    @staticmethod
    def _scene_index(path: Path) -> int:
        try:
            return int(path.name.split("_", 1)[1])
        except (IndexError, ValueError):
            return 0

    @staticmethod
    def _fingerprint(path: Path) -> str:
        stat = path.stat()
        return f"{stat.st_size}:{stat.st_mtime_ns}"

    @staticmethod
    def _read_json(path: Path, default: Any) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default
