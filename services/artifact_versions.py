from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from uuid import uuid4

from domain.artifacts import (
    ArtifactStatus,
    ArtifactType,
    ArtifactVersion,
    compute_input_hash,
)
from repositories.artifact_repository import ArtifactRepository
from project_identity import state_directory


_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


class ArtifactVersionService:
    """Stores immutable file snapshots and coordinates metadata activation."""

    def __init__(self, repository: ArtifactRepository, workspace_root: str | Path,
                 external_roots_provider: Callable[[], Iterable[str | Path]] | None = None) -> None:
        self.repository = repository
        self.workspace_root = Path(workspace_root).resolve()
        self.external_roots_provider = external_roots_provider
        self.version_root = state_directory(self.workspace_root) / "artifact_versions"
        self.version_root.mkdir(parents=True, exist_ok=True)

    def record_file(
        self,
        project_id: str,
        scene_index: int,
        shot_index: int,
        artifact_type: ArtifactType | str,
        source_path: str | Path,
        *,
        input_values: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ArtifactVersion:
        source = self._workspace_path(source_path)
        if not source.is_file():
            raise FileNotFoundError(source)
        normalized_type = ArtifactType(artifact_type)
        content_sha256 = ""
        if normalized_type is ArtifactType.KEYFRAME:
            content_sha256 = _file_sha256(source)
            existing = self._find_content_version(
                project_id,
                scene_index,
                shot_index,
                normalized_type,
                content_sha256,
            )
            if existing is not None:
                if existing.status is ArtifactStatus.ACTIVE:
                    return existing
                return self.repository.activate_version(existing.artifact_id)
        snapshot = self._snapshot_path(
            project_id, scene_index, shot_index, normalized_type, source.suffix)
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        temporary = snapshot.with_name(f".{snapshot.name}.{uuid4().hex}.tmp")
        shutil.copy2(source, temporary)
        os.replace(temporary, snapshot)
        details = dict(metadata or {})
        if content_sha256:
            details["content_sha256"] = content_sha256
        return self._register_snapshot(
            project_id,
            scene_index,
            shot_index,
            normalized_type,
            snapshot,
            source,
            input_values,
            details,
        )

    def record_content(
        self,
        project_id: str,
        scene_index: int,
        shot_index: int,
        artifact_type: ArtifactType | str,
        content: str | bytes,
        *,
        live_path: str | Path,
        suffix: str = ".json",
        input_values: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ArtifactVersion:
        live = self._workspace_path(live_path)
        payload = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        snapshot = self._snapshot_path(
            project_id, scene_index, shot_index, artifact_type, suffix)
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        temporary = snapshot.with_name(f".{snapshot.name}.{uuid4().hex}.tmp")
        temporary.write_bytes(payload)
        os.replace(temporary, snapshot)
        values = dict(input_values or {})
        values.setdefault("content", hashlib.sha256(payload).hexdigest())
        return self._register_snapshot(
            project_id,
            scene_index,
            shot_index,
            artifact_type,
            snapshot,
            live,
            values,
            metadata,
        )

    def record_json_item(
        self,
        project_id: str,
        scene_index: int,
        shot_index: int,
        item: Any,
        *,
        live_path: str | Path,
        input_values: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ArtifactVersion:
        details = dict(metadata or {})
        details["json_list_index"] = int(shot_index)
        content = json.dumps(item, ensure_ascii=False, sort_keys=True, indent=2)
        return self.record_content(
            project_id,
            scene_index,
            shot_index,
            ArtifactType.STORYBOARD,
            content,
            live_path=live_path,
            suffix=".json",
            input_values=input_values,
            metadata=details,
        )

    def list_versions(
        self,
        project_id: str,
        scene_index: int,
        shot_index: int,
        artifact_type: ArtifactType | str,
    ) -> list[ArtifactVersion]:
        normalized_type = ArtifactType(artifact_type)
        versions = self.repository.list_versions(
            project_id, scene_index, shot_index, normalized_type)
        if normalized_type is not ArtifactType.KEYFRAME:
            return versions

        unique: list[ArtifactVersion] = []
        seen: set[str] = set()
        for version in versions:
            content_sha256 = self._version_content_sha256(version)
            if content_sha256 and content_sha256 in seen:
                continue
            if content_sha256:
                seen.add(content_sha256)
            unique.append(version)
        return unique

    def _find_content_version(
        self,
        project_id: str,
        scene_index: int,
        shot_index: int,
        artifact_type: ArtifactType,
        content_sha256: str,
    ) -> ArtifactVersion | None:
        for version in self.repository.list_versions(
            project_id, scene_index, shot_index, artifact_type
        ):
            if self._version_content_sha256(version) == content_sha256:
                return version
        return None

    def _version_content_sha256(self, version: ArtifactVersion) -> str:
        stored = str(version.metadata.get("content_sha256") or "").strip().lower()
        if stored:
            return stored
        try:
            return _file_sha256(self._workspace_path(version.relative_path))
        except (FileNotFoundError, OSError, ValueError):
            return ""

    def resolve_version_path(self, artifact_id: str) -> Path:
        version = self.repository.get_version(artifact_id)
        if version is None:
            raise KeyError(f"artifact version not found: {artifact_id}")
        path = self._workspace_path(version.relative_path)
        if not path.is_file():
            raise FileNotFoundError(path)
        return path

    def rollback(self, artifact_id: str) -> ArtifactVersion:
        target = self.repository.get_version(artifact_id)
        if target is None:
            raise KeyError(f"artifact version not found: {artifact_id}")
        snapshot = self.resolve_version_path(artifact_id)
        live_value = target.metadata.get("live_relative_path")
        if not live_value:
            raise ValueError("artifact version has no live_relative_path")
        live = self._workspace_path(str(live_value))
        live.parent.mkdir(parents=True, exist_ok=True)
        lock = _path_lock(live)

        with lock:
            replacement = live.with_name(f".{live.name}.{uuid4().hex}.rollback")
            backup = live.with_name(f".{live.name}.{uuid4().hex}.backup")
            json_index = target.metadata.get("json_list_index")
            if json_index is None:
                shutil.copy2(snapshot, replacement)
            else:
                self._prepare_json_item_rollback(
                    snapshot, live, replacement, int(json_index))
            had_live = live.exists()
            if had_live:
                shutil.copy2(live, backup)
            try:
                os.replace(replacement, live)
                activated = self.repository.activate_version(artifact_id)
            except BaseException:
                replacement.unlink(missing_ok=True)
                if had_live and backup.exists():
                    os.replace(backup, live)
                elif not had_live:
                    live.unlink(missing_ok=True)
                raise
            finally:
                backup.unlink(missing_ok=True)
            return activated

    @staticmethod
    def _prepare_json_item_rollback(
        snapshot: Path,
        live: Path,
        replacement: Path,
        index: int,
    ) -> None:
        if index < 0:
            raise ValueError("json_list_index must be non-negative")
        try:
            current = json.loads(live.read_text(encoding="utf-8"))
            item = json.loads(snapshot.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("cannot roll back JSON artifact") from exc
        if not isinstance(current, list) or index >= len(current):
            raise ValueError("live JSON artifact no longer contains the versioned item")
        current[index] = item
        replacement.write_text(
            json.dumps(current, ensure_ascii=False, indent=4), encoding="utf-8")

    def mark_inputs_changed(
        self,
        project_id: str,
        scene_index: int,
        shot_index: int,
        changed_type: ArtifactType | str,
        input_values: Mapping[str, Any],
        *,
        reason: str,
    ) -> list[ArtifactVersion]:
        return self.repository.mark_inputs_changed(
            project_id,
            scene_index,
            shot_index,
            changed_type,
            input_hash=compute_input_hash(input_values),
            reason=reason,
        )

    def _register_snapshot(
        self,
        project_id: str,
        scene_index: int,
        shot_index: int,
        artifact_type: ArtifactType | str,
        snapshot: Path,
        live: Path,
        input_values: Mapping[str, Any] | None,
        metadata: Mapping[str, Any] | None,
    ) -> ArtifactVersion:
        values = dict(input_values or {})
        input_hashes = {name: compute_input_hash(value) for name, value in values.items()}
        combined_hash = compute_input_hash(values)
        details = dict(metadata or {})
        details["live_relative_path"] = self._relative(live)
        try:
            return self.repository.create_version(
                project_id,
                scene_index,
                shot_index,
                artifact_type,
                input_hash=combined_hash,
                relative_path=self._relative(snapshot),
                inputs=input_hashes,
                metadata=details,
            )
        except BaseException:
            snapshot.unlink(missing_ok=True)
            raise

    def _snapshot_path(
        self,
        project_id: str,
        scene_index: int,
        shot_index: int,
        artifact_type: ArtifactType | str,
        suffix: str,
    ) -> Path:
        artifact_type = ArtifactType(artifact_type)
        project_component = _safe_component(project_id)
        normalized_suffix = suffix if str(suffix).startswith(".") else f".{suffix}"
        return (
            self.version_root
            / project_component
            / f"scene_{int(scene_index)}"
            / f"shot_{int(shot_index)}"
            / artifact_type.value
            / f"{uuid4().hex}{normalized_suffix}"
        )

    def _workspace_path(self, value: str | Path) -> Path:
        candidate = Path(value)
        resolved = candidate.resolve() if candidate.is_absolute() else (self.workspace_root / candidate).resolve()
        if not any(_inside(resolved, root) for root in self._allowed_roots()):
            raise ValueError("artifact path must stay within the workspace or a configured media directory")
        return resolved

    def _relative(self, path: Path) -> str:
        resolved = path.resolve()
        try:
            return resolved.relative_to(self.workspace_root).as_posix()
        except ValueError:
            if any(_inside(resolved, root) for root in self._allowed_roots()):
                return str(resolved)
            raise ValueError("artifact path must stay within the workspace or a configured media directory")

    def _allowed_roots(self) -> list[Path]:
        roots = [self.workspace_root]
        if self.external_roots_provider is not None:
            try:
                roots.extend(Path(item).expanduser().resolve() for item in self.external_roots_provider())
            except Exception:
                pass
        return list(dict.fromkeys(roots))


def _safe_component(value: str) -> str:
    original = str(value or "").strip()
    if not original:
        raise ValueError("project_id cannot be empty")
    readable = re.sub(r"[^A-Za-z0-9._-]+", "_", original).strip("._")[:48]
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:12]
    return f"{readable or 'project'}-{digest}"


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _path_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
