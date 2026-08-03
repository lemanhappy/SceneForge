from __future__ import annotations

import json
import logging
import os
import re
import threading
from contextlib import contextmanager
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any

from repositories.session_state_store import SessionStateStore
from project_identity import state_directory

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None

# Process-wide lock per sessions file path. fcntl.flock (POSIX) serialises
# cross-process writers, but is unavailable on Windows where _locked would
# otherwise be a no-op and concurrent threads race os.replace. This in-process
# lock serialises threads/async tasks on every platform.
_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


def _process_lock_for(path: str) -> threading.Lock:
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(path)
        if lock is None:
            lock = threading.Lock()
            _PROCESS_LOCKS[path] = lock
        return lock


STALE_KEYS = ["story", "characters", "script", "storyboard", "shot_descriptions", "camera_tree", "frames", "clips", "final_video"]


def _synchronized(method):
    """Hold the index file lock across a read-modify-write cycle.

    Every mutator loads the whole sessions file, edits it, and saves it back;
    without a lock, two concurrent writers (threads or processes) silently
    drop each other's updates.
    """

    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._locked():
            return method(self, *args, **kwargs)

    return wrapper


class SessionIndex:
    def __init__(self, workspace_root: str | Path, state_store: SessionStateStore | None = None) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.state_store = state_store
        self.sceneforge_dir = state_directory(self.workspace_root)
        self.sessions_path = self.sceneforge_dir / "sessions.json"
        self.memory_path = self.sceneforge_dir / "memory.md"
        self.logs_dir = self.sceneforge_dir / "logs"
        self.working_root = self.workspace_root / ".working_dir"
        self.sceneforge_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.working_root.mkdir(parents=True, exist_ok=True)
        if not self.memory_path.exists():
            self.memory_path.write_text("# User Preferences\n", encoding="utf-8")
        if self.state_store is None and not self.sessions_path.exists():
            self.save({"active_session_id": "", "sessions": {}})

    @contextmanager
    def _locked(self):
        if self.state_store is not None:
            with self.state_store.locked():
                yield
            return
        # In-process serialisation (all platforms) + cross-process flock (POSIX).
        process_lock = _process_lock_for(str(self.sessions_path.resolve()))
        process_lock.acquire()
        try:
            if fcntl is None:
                yield
                return
            lock_path = self.sceneforge_dir / "sessions.lock"
            with open(lock_path, "a+", encoding="utf-8") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            process_lock.release()

    def load(self) -> dict[str, Any]:
        if self.state_store is not None:
            return self.state_store.load()
        try:
            return json.loads(self.sessions_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"active_session_id": "", "sessions": {}}
        except json.JSONDecodeError:
            # A corrupt file usually means a crash mid-write. Returning empty
            # state is fine for this call, but the next save() would overwrite
            # the file and destroy every session — keep the evidence first.
            backup = self.sessions_path.with_name(f"sessions.json.corrupt-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}")
            try:
                os.replace(self.sessions_path, backup)
                logging.error("sessions.json was corrupt; preserved it at %s and starting with empty state", backup)
            except OSError:
                logging.error("sessions.json is corrupt and could not be backed up; starting with empty state")
            return {"active_session_id": "", "sessions": {}}

    def save(self, data: dict[str, Any]) -> None:
        if self.state_store is not None:
            self.state_store.save(data)
            return
        tmp_path = self.sessions_path.with_name("sessions.json.tmp")
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, self.sessions_path)

    def active(self) -> dict[str, Any] | None:
        data = self.load()
        session_id = str(data.get("active_session_id", ""))
        if not session_id:
            return None
        record = data.get("sessions", {}).get(session_id)
        return self._with_session_defaults(record) if isinstance(record, dict) else None

    def get(self, session_id: str) -> dict[str, Any] | None:
        normalized = self._normalize_session_id(session_id)
        record = self.load().get("sessions", {}).get(normalized)
        return self._with_session_defaults(record) if isinstance(record, dict) else None

    def list_sessions(self) -> list[dict[str, Any]]:
        """Return complete session records without exposing the JSON envelope."""
        records: list[dict[str, Any]] = []
        for session_id, record in (self.load().get("sessions") or {}).items():
            if not isinstance(record, dict):
                continue
            item = self._with_session_defaults(record)
            item.setdefault("session_id", session_id)
            records.append(item)
        return sorted(records, key=lambda item: str(item.get("updated_at") or ""), reverse=True)

    @_synchronized
    def create(self, idea: str = "", user_requirement: str = "", style: str = "", session_id: str | None = None, domain: str = "", character_asset_ids: list | None = None, prop_asset_ids: list | None = None, scene_asset_ids: list | None = None, lora_ids: list | None = None, lora_bindings: list | None = None, mode: str = "idea", script: str = "", target_language: str | None = None, aspect_ratio: str | None = None, overrides: dict | None = None, quality_tier: str = "balanced", config_snapshot: dict | None = None, continuity_source_session_id: str | None = None) -> dict[str, Any]:
        data = self.load()
        sessions = data.setdefault("sessions", {})
        final_id = self._normalize_session_id(session_id) if session_id else self._new_session_id(idea or user_requirement or "sceneforge", sessions)
        if final_id in sessions:
            final_id = self._dedupe_session_id(final_id, sessions)
        now = datetime.now().isoformat(timespec="seconds")
        working_dir = self._working_dir_for_id(final_id)
        (working_dir / "idea2video").mkdir(parents=True, exist_ok=True)
        (working_dir / "script2video").mkdir(parents=True, exist_ok=True)
        record = {
            "session_id": final_id,
            "working_dir": self._serialize_working_dir(working_dir),
            "storage_root": str(self.working_root),
            "idea": idea,
            "user_requirement": user_requirement,
            "style": style,
            "domain": domain,
            "character_asset_ids": list(character_asset_ids or []),
            "prop_asset_ids": list(prop_asset_ids or []),
            "scene_asset_ids": list(scene_asset_ids or []),
            "lora_ids": list(lora_ids or []),
            "lora_bindings": list(lora_bindings or []),
            "mode": "script" if mode == "script" else "idea",
            "script": script,
            # per-video overrides (None/absent -> fall back to the global config at
            # render time). target_language/aspect_ratio kept top-level for back-compat;
            # other per-video audio/subtitle choices live in `overrides`.
            "target_language": target_language,
            "aspect_ratio": aspect_ratio,
            "overrides": dict(overrides or {}),
            "quality_tier": quality_tier if quality_tier in {"economy", "balanced", "quality"} else "balanced",
            "config_snapshot": dict(config_snapshot or {}),
            "continuity_source_session_id": str(continuity_source_session_id or ""),
            "stage": "created",
            "summary": "",
            "stale": {key: False for key in STALE_KEYS},
            "recent_turn_records": [],
            "compacted_summary": "",
            "compacted_turns": 0,
            "compaction_snapshots": [],
            "last_compaction_reason": "",
            "last_compaction_at": "",
            "created_at": now,
            "updated_at": now,
        }
        sessions[final_id] = record
        data["active_session_id"] = final_id
        self.save(data)
        return record

    def get_or_create_active(self, idea: str = "", user_requirement: str = "", style: str = "") -> dict[str, Any]:
        active = self.active()
        if active is not None:
            return active
        return self.create(idea=idea, user_requirement=user_requirement, style=style)

    @_synchronized
    def set_active(self, session_id: str) -> dict[str, Any]:
        normalized = self._normalize_session_id(session_id)
        data = self.load()
        if normalized not in data.get("sessions", {}):
            raise KeyError(f"Unknown session_id: {session_id}")
        data["active_session_id"] = normalized
        self.save(data)
        return dict(data["sessions"][normalized])

    @_synchronized
    def update_stage(self, session_id: str, stage: str, summary: str = "") -> None:
        data = self.load()
        record = data.get("sessions", {}).get(session_id)
        if not isinstance(record, dict):
            raise KeyError(f"Unknown session_id: {session_id}")
        record["stage"] = stage
        if summary:
            record["summary"] = summary
        record["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self.save(data)

    @_synchronized
    def update_metadata(
        self,
        session_id: str,
        *,
        idea: str = "",
        user_requirement: str = "",
        style: str = "",
        **metadata: Any,
    ) -> dict[str, Any]:
        data = self.load()
        record = data.get("sessions", {}).get(session_id)
        if not isinstance(record, dict):
            raise KeyError(f"Unknown session_id: {session_id}")
        if idea and not record.get("idea"):
            record["idea"] = idea
        if user_requirement:
            record["user_requirement"] = user_requirement
        if style:
            record["style"] = style
        protected = {"session_id", "working_dir", "created_at", "review_tasks"}
        for key, value in metadata.items():
            if key not in protected:
                record[key] = value
        record["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self.save(data)
        return self._with_session_defaults(record)

    @_synchronized
    def mark_stale(self, session_id: str, keys: list[str]) -> None:
        data = self.load()
        record = data.get("sessions", {}).get(session_id)
        if not isinstance(record, dict):
            raise KeyError(f"Unknown session_id: {session_id}")
        stale = record.setdefault("stale", {key: False for key in STALE_KEYS})
        for key in keys:
            stale[key] = True
        record["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self.save(data)

    @_synchronized
    def create_review_task(self, session_id: str, stage: str, summary: str = "", artifact_refs: list[str] | None = None, artifact_version: str = "v1") -> dict[str, Any]:
        data = self.load()
        record = data.get("sessions", {}).get(session_id)
        if not isinstance(record, dict):
            raise KeyError(f"Unknown session_id: {session_id}")
        tasks = record.setdefault("review_tasks", [])
        now = datetime.now().isoformat(timespec="seconds")
        task = {
            "review_id": f"rev_{len(tasks) + 1}_{stage}",
            "session_id": session_id,
            "stage": stage,
            "artifact_version": artifact_version,
            "status": "pending",
            "summary": summary,
            "artifact_refs": list(artifact_refs or []),
            "created_at": now,
            "resolved_at": None,
        }
        tasks.append(task)
        record["updated_at"] = now
        self.save(data)
        return task

    def list_review_tasks(self, session_id: str) -> list[dict[str, Any]]:
        record = self.load().get("sessions", {}).get(session_id)
        if not isinstance(record, dict):
            return []
        return list(record.get("review_tasks", []) or [])

    def get_review_task(self, session_id: str, review_id: str) -> dict[str, Any] | None:
        for task in self.list_review_tasks(session_id):
            if task.get("review_id") == review_id:
                return task
        return None

    @_synchronized
    def resolve_review_task(self, session_id: str, review_id: str, status: str) -> dict[str, Any]:
        data = self.load()
        record = data.get("sessions", {}).get(session_id)
        if not isinstance(record, dict):
            raise KeyError(f"Unknown session_id: {session_id}")
        for task in record.get("review_tasks", []) or []:
            if task.get("review_id") == review_id:
                now = datetime.now().isoformat(timespec="seconds")
                task["status"] = status
                task["resolved_at"] = now
                record["updated_at"] = now
                self.save(data)
                return task
        raise KeyError(f"Unknown review_id: {review_id}")

    @_synchronized
    def update_compaction(self, session_id: str, result: dict[str, Any]) -> None:
        data = self.load()
        session = data.get("sessions", {}).get(session_id)
        if not isinstance(session, dict):
            raise KeyError(f"Unknown session_id: {session_id}")
        summary = str(result.get("summary", "") or "")
        compacted_count = int(result.get("compacted_message_count", 0) or 0)
        snapshot = {
            "level": len(session.get("compaction_snapshots", []) or []) + 1,
            "reason": str(result.get("reason", "manual") or "manual"),
            "mode": str(result.get("mode", "unknown") or "unknown"),
            "summary": summary,
            "preserved_messages": int(result.get("preserved_message_count", 0) or 0),
            "compacted_message_count": compacted_count,
            "estimated_tokens_before": int(result.get("estimated_tokens_before", 0) or 0),
            "estimated_tokens_after": int(result.get("estimated_tokens_after", 0) or 0),
            "created_at": str(result.get("created_at", "") or datetime.now().isoformat(timespec="seconds")),
        }
        session["compacted_summary"] = summary
        session["compacted_turns"] = int(session.get("compacted_turns", 0) or 0) + max(1, compacted_count // 2)
        snapshots = list(session.get("compaction_snapshots", []) or [])
        snapshots.append(snapshot)
        session["compaction_snapshots"] = snapshots[-8:]
        session["last_compaction_reason"] = snapshot["reason"]
        session["last_compaction_at"] = snapshot["created_at"]
        session["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self.save(data)
        self.append_log("loop_history", {"session_id": session_id, "event": "context_compacted", "compaction": snapshot})

    def compacted_summary(self, session_id: str | None = None) -> str:
        record = self.get(session_id) if session_id else self.active()
        return str((record or {}).get("compacted_summary", "") or "")

    @_synchronized
    def append_turn_record(self, session_id: str, record: dict[str, Any]) -> None:
        data = self.load()
        session = data.get("sessions", {}).get(session_id)
        if isinstance(session, dict):
            recent = session.setdefault("recent_turn_records", [])
            recent.append({
                "turn_id": record.get("turn_id", ""),
                "status": record.get("status", ""),
                "tool_round_count": len(record.get("tool_rounds", [])),
                "final_preview": str(record.get("final_assistant_text", ""))[:240],
                "created_at": record.get("created_at", ""),
            })
            session["recent_turn_records"] = recent[-6:]
            session["updated_at"] = datetime.now().isoformat(timespec="seconds")
            self.save(data)
        self.append_log("loop_history", {"session_id": session_id, **record})

    @_synchronized
    def delete(self, session_id: str) -> bool:
        """Remove a session from the index and delete its working dir (and the
        review tasks nested in its record). Returns True if a session was removed."""
        normalized = self._normalize_session_id(session_id)
        data = self.load()
        sessions = data.get("sessions", {})
        rec = sessions.pop(normalized, None)
        if rec is None:
            return False
        if str(data.get("active_session_id", "")) == normalized:
            data["active_session_id"] = ""
        self.save(data)
        # Delete only inside the storage root captured when this project was
        # created. This keeps older projects valid after the user changes the
        # default media directory.
        try:
            import shutil
            wd, allowed_root = self._resolve_record_working_dir(rec)
            if wd != allowed_root and allowed_root in wd.parents and wd.exists():
                shutil.rmtree(wd, ignore_errors=True)
        except Exception:
            logging.exception("Failed to remove working dir for session %s", normalized)
        return True

    def working_dir(self, session_id: str | None = None) -> Path:
        record = self.get(session_id) if session_id else self.active()
        if record is None:
            record = self.create()
        path, _allowed_root = self._resolve_record_working_dir(record)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def set_working_root(self, path: str | Path) -> Path:
        """Change where newly-created projects store generated media.

        Existing records retain their own ``storage_root`` and continue to
        resolve from the directory in which they were created.
        """
        resolved = Path(path).expanduser().resolve()
        resolved.mkdir(parents=True, exist_ok=True)
        self.working_root = resolved
        return resolved

    def _serialize_working_dir(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.workspace_root))
        except ValueError:
            return str(path)

    def _resolve_record_working_dir(self, record: dict[str, Any]) -> tuple[Path, Path]:
        raw = Path(str(record.get("working_dir") or ""))
        path = raw.resolve() if raw.is_absolute() else (self.workspace_root / raw).resolve()
        stored_root = str(record.get("storage_root") or "").strip()
        allowed_root = (Path(stored_root).expanduser().resolve()
                        if stored_root else (self.workspace_root / ".working_dir").resolve())
        if path == allowed_root or allowed_root not in path.parents:
            raise ValueError(f"Session working_dir escapes its storage root: {record.get('working_dir')}")
        return path, allowed_root

    def artifact_checklist(self, session_id: str | None = None) -> dict[str, bool]:
        root = self.working_dir(session_id)
        idea_dir = root / "idea2video"
        idea_scene_dirs = sorted(path for path in idea_dir.glob("scene_*") if path.is_dir()) if idea_dir.exists() else []
        idea_scene_storyboards = [path / "storyboard.json" for path in idea_scene_dirs]
        idea_scene_camera_trees = [path / "camera_tree.json" for path in idea_scene_dirs]
        idea_scene_shot_desc_groups = [list((scene / "shots").glob("*/shot_description.json")) for scene in idea_scene_dirs]
        idea_scene_selector_outputs = [output for scene in idea_scene_dirs for output in (scene / "shots").glob("*/*_selector_output.json")]

        script_shots = root / "script2video" / "shots"
        script_shot_descs = list(script_shots.glob("*/shot_description.json")) if script_shots.exists() else []
        script_selector_outputs = list(script_shots.glob("*/*_selector_output.json")) if script_shots.exists() else []

        novel_dir = root / "novel2video"
        novel_events = list((novel_dir / "events").glob("event_*.json")) if novel_dir.exists() else []
        novel_relevant_chunks = [path for path in (novel_dir / "relevant_chunks").glob("event_*/*") if path.is_file()] if novel_dir.exists() else []
        novel_scenes = list((novel_dir / "scenes").glob("event_*/scene_*.json")) if novel_dir.exists() else []
        novel_event_chars = list((novel_dir / "global_information" / "characters" / "event_level").glob("event_*_characters.json")) if novel_dir.exists() else []
        novel_level_chars = list((novel_dir / "global_information" / "characters" / "novel_level").glob("novel_characters_after_event_*.json")) if novel_dir.exists() else []
        return {
            "idea2video/story.txt": (idea_dir / "story.txt").exists(),
            "idea2video/characters.json": (idea_dir / "characters.json").exists(),
            "idea2video/script.json": (idea_dir / "script.json").exists(),
            "idea2video/scene_*/storyboard.json": bool(idea_scene_storyboards) and all(path.exists() for path in idea_scene_storyboards),
            "idea2video/scene_*/camera_tree.json": bool(idea_scene_camera_trees) and all(path.exists() for path in idea_scene_camera_trees),
            "idea2video/scene_*/shots/*/shot_description.json": bool(idea_scene_shot_desc_groups) and all(idea_scene_shot_desc_groups),
            "idea2video/scene_*/shots/*/*_selector_output.json": bool(idea_scene_selector_outputs),
            "idea2video/final_video.mp4": (idea_dir / "final_video.mp4").exists(),
            "script2video/characters.json": (root / "script2video" / "characters.json").exists(),
            "script2video/storyboard.json": (root / "script2video" / "storyboard.json").exists(),
            "script2video/shots/*/shot_description.json": bool(script_shot_descs),
            "script2video/camera_tree.json": (root / "script2video" / "camera_tree.json").exists(),
            "script2video/shots/*/*_selector_output.json": bool(script_selector_outputs),
            "script2video/final_video.mp4": (root / "script2video" / "final_video.mp4").exists(),
            "novel2video/novel/novel.txt": (novel_dir / "novel" / "novel.txt").exists(),
            "novel2video/novel/novel_compressed.txt": (novel_dir / "novel" / "novel_compressed.txt").exists(),
            "novel2video/events/event_*.json": bool(novel_events),
            "novel2video/relevant_chunks/event_*": bool(novel_relevant_chunks),
            "novel2video/scenes/event_*/scene_*.json": bool(novel_scenes),
            "novel2video/global_information/characters/event_level/*.json": bool(novel_event_chars),
            "novel2video/global_information/characters/novel_level/*.json": bool(novel_level_chars),
        }

    def memory_text(self) -> str:
        return self.memory_path.read_text(encoding="utf-8") if self.memory_path.exists() else ""

    def write_memory(self, text: str) -> None:
        self.memory_path.write_text(text, encoding="utf-8")

    def append_log(self, name: str, payload: dict[str, Any]) -> None:
        event = {"timestamp": datetime.now().isoformat(timespec="seconds"), **payload}
        path = self.logs_dir / f"{name}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")

    def snapshot(self) -> dict[str, Any]:
        active = self.active()
        if active is None:
            return {"active_session_id": "", "session": None}
        return {"active_session_id": active["session_id"], "session": active, "artifact_checklist": self.artifact_checklist(active["session_id"])}

    def _with_session_defaults(self, record: dict[str, Any]) -> dict[str, Any]:
        item = dict(record)
        item.setdefault("compacted_summary", "")
        item.setdefault("compacted_turns", 0)
        item.setdefault("compaction_snapshots", [])
        item.setdefault("last_compaction_reason", "")
        item.setdefault("last_compaction_at", "")
        item.setdefault("recent_turn_records", [])
        item.setdefault("review_tasks", [])
        item.setdefault("character_asset_ids", [])
        item.setdefault("prop_asset_ids", [])
        item.setdefault("scene_asset_ids", [])
        item.setdefault("lora_ids", [])
        item.setdefault("lora_bindings", [])
        item.setdefault("config_snapshot", {})
        item.setdefault("continuity_source_session_id", "")
        return item

    def _new_session_id(self, source: str, sessions: dict[str, Any]) -> str:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", source.lower()).strip("-")[:32] or "sceneforge"
        return self._dedupe_session_id(f"{stamp}-{slug}", sessions)

    def _dedupe_session_id(self, base: str, sessions: dict[str, Any]) -> str:
        candidate = base
        counter = 2
        while candidate in sessions:
            candidate = f"{base}-{counter}"
            counter += 1
        return candidate

    def _normalize_session_id(self, session_id: str | None) -> str:
        raw = str(session_id or "").strip()
        if not raw:
            raise ValueError("session_id cannot be empty")
        normalized = re.sub(r"[^a-zA-Z0-9]+", "-", raw).strip("-")[:96]
        if not normalized:
            raise ValueError(f"Invalid session_id: {session_id}")
        return normalized

    def _working_dir_for_id(self, session_id: str) -> Path:
        path = (self.working_root / session_id).resolve()
        if path != self.working_root and self.working_root not in path.parents:
            raise ValueError(f"Session path escapes .working_dir: {session_id}")
        return path
