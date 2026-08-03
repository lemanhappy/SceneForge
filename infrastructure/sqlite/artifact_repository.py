from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Mapping
from uuid import uuid4

from domain.artifacts import (
    ArtifactStatus,
    ArtifactType,
    ArtifactVersion,
    ShotReadiness,
    ShotState,
    affected_artifact_types,
)

from .database import SQLiteDatabase


class SQLiteArtifactRepository:
    """Transactional shot state and immutable artifact version metadata."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database
        self.database.migrate()

    def get_shot_state(
        self, project_id: str, scene_index: int, shot_index: int
    ) -> ShotState | None:
        project_id, scene_index, shot_index = _shot_key(
            project_id, scene_index, shot_index)
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM shots
                WHERE project_id = ? AND scene_index = ? AND shot_index = ?
                """,
                (project_id, scene_index, shot_index),
            ).fetchone()
        return _row_to_shot_state(row) if row is not None else None

    def set_shot_state(
        self,
        project_id: str,
        scene_index: int,
        shot_index: int,
        readiness: ShotReadiness | str,
        *,
        input_hash: str = "",
        stale_reason: str | None = None,
    ) -> ShotState:
        project_id, scene_index, shot_index = _shot_key(
            project_id, scene_index, shot_index)
        normalized = ShotReadiness(readiness)
        now = _utc_now()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO shots(
                    project_id, scene_index, shot_index, readiness,
                    input_hash, stale_reason, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, scene_index, shot_index) DO UPDATE SET
                    readiness = excluded.readiness,
                    input_hash = excluded.input_hash,
                    stale_reason = excluded.stale_reason,
                    updated_at = excluded.updated_at
                """,
                (
                    project_id,
                    scene_index,
                    shot_index,
                    normalized.value,
                    str(input_hash or ""),
                    stale_reason,
                    now,
                ),
            )
            row = _get_shot_row(connection, project_id, scene_index, shot_index)
        return _row_to_shot_state(row)

    def create_version(
        self,
        project_id: str,
        scene_index: int,
        shot_index: int,
        artifact_type: ArtifactType | str,
        *,
        input_hash: str,
        relative_path: str,
        inputs: Mapping[str, str] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ArtifactVersion:
        project_id, scene_index, shot_index = _shot_key(
            project_id, scene_index, shot_index)
        normalized_type = ArtifactType(artifact_type)
        normalized_path = _relative_path(relative_path)
        normalized_hash = str(input_hash or "").strip()
        if not normalized_hash:
            raise ValueError("input_hash cannot be empty")
        normalized_inputs = {
            str(name): str(value) for name, value in (inputs or {}).items()
        }
        metadata_json = json.dumps(
            dict(metadata or {}), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        artifact_id = f"artifact_{uuid4().hex}"
        now = _utc_now()

        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                """
                SELECT COALESCE(MAX(version), 0) AS latest
                FROM artifacts
                WHERE project_id = ? AND scene_index = ? AND shot_index = ?
                  AND artifact_type = ?
                """,
                (project_id, scene_index, shot_index, normalized_type.value),
            ).fetchone()
            version = int(row["latest"]) + 1
            connection.execute(
                """
                UPDATE artifacts SET status = ?
                WHERE project_id = ? AND scene_index = ? AND shot_index = ?
                  AND artifact_type = ? AND status = ?
                """,
                (
                    ArtifactStatus.ARCHIVED.value,
                    project_id,
                    scene_index,
                    shot_index,
                    normalized_type.value,
                    ArtifactStatus.ACTIVE.value,
                ),
            )
            self._mark_types_stale(
                connection,
                project_id,
                scene_index,
                shot_index,
                affected_artifact_types(normalized_type, include_changed=False),
            )
            connection.execute(
                """
                INSERT INTO artifacts(
                    artifact_id, project_id, scene_index, shot_index,
                    artifact_type, version, status, input_hash, relative_path,
                    metadata_json, created_at, activated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    project_id,
                    scene_index,
                    shot_index,
                    normalized_type.value,
                    version,
                    ArtifactStatus.ACTIVE.value,
                    normalized_hash,
                    normalized_path,
                    metadata_json,
                    now,
                    now,
                ),
            )
            for name, value in sorted(normalized_inputs.items()):
                connection.execute(
                    "INSERT INTO artifact_inputs(artifact_id, input_name, input_hash) VALUES (?, ?, ?)",
                    (artifact_id, name, value),
                )
            self._upsert_state(
                connection,
                project_id,
                scene_index,
                shot_index,
                _readiness_after_generation(normalized_type),
                normalized_hash,
                None,
                now,
            )
            artifact_row = _get_artifact_row(connection, artifact_id)
            result = self._row_to_version(connection, artifact_row)
        return result

    def get_version(self, artifact_id: str) -> ArtifactVersion | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
            return self._row_to_version(connection, row) if row is not None else None

    def list_versions(
        self,
        project_id: str,
        scene_index: int,
        shot_index: int,
        artifact_type: ArtifactType | str,
    ) -> list[ArtifactVersion]:
        project_id, scene_index, shot_index = _shot_key(
            project_id, scene_index, shot_index)
        normalized_type = ArtifactType(artifact_type)
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM artifacts
                WHERE project_id = ? AND scene_index = ? AND shot_index = ?
                  AND artifact_type = ?
                ORDER BY version DESC
                """,
                (project_id, scene_index, shot_index, normalized_type.value),
            ).fetchall()
            return [self._row_to_version(connection, row) for row in rows]

    def mark_inputs_changed(
        self,
        project_id: str,
        scene_index: int,
        shot_index: int,
        changed_type: ArtifactType | str,
        *,
        input_hash: str,
        reason: str,
    ) -> list[ArtifactVersion]:
        project_id, scene_index, shot_index = _shot_key(
            project_id, scene_index, shot_index)
        normalized_type = ArtifactType(changed_type)
        affected = affected_artifact_types(normalized_type)
        now = _utc_now()
        with self.database.transaction(immediate=True) as connection:
            changed_ids = self._mark_types_stale(
                connection, project_id, scene_index, shot_index, affected)
            self._upsert_state(
                connection,
                project_id,
                scene_index,
                shot_index,
                ShotReadiness.STALE,
                str(input_hash or ""),
                str(reason or "inputs_changed"),
                now,
            )
            return [
                self._row_to_version(connection, _get_artifact_row(connection, artifact_id))
                for artifact_id in changed_ids
            ]

    def activate_version(self, artifact_id: str) -> ArtifactVersion:
        now = _utc_now()
        with self.database.transaction(immediate=True) as connection:
            target = connection.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
            if target is None:
                raise KeyError(f"artifact version not found: {artifact_id}")
            artifact_type = ArtifactType(target["artifact_type"])
            key = (
                target["project_id"],
                target["scene_index"],
                target["shot_index"],
                target["artifact_type"],
            )
            connection.execute(
                """
                UPDATE artifacts SET status = ?
                WHERE project_id = ? AND scene_index = ? AND shot_index = ?
                  AND artifact_type = ? AND status = ? AND artifact_id <> ?
                """,
                (
                    ArtifactStatus.ARCHIVED.value,
                    *key,
                    ArtifactStatus.ACTIVE.value,
                    artifact_id,
                ),
            )
            self._mark_types_stale(
                connection,
                target["project_id"],
                target["scene_index"],
                target["shot_index"],
                affected_artifact_types(artifact_type, include_changed=False),
            )
            connection.execute(
                "UPDATE artifacts SET status = ?, activated_at = ? WHERE artifact_id = ?",
                (ArtifactStatus.ACTIVE.value, now, artifact_id),
            )
            self._upsert_state(
                connection,
                target["project_id"],
                target["scene_index"],
                target["shot_index"],
                _readiness_after_generation(artifact_type),
                target["input_hash"],
                None,
                now,
            )
            return self._row_to_version(connection, _get_artifact_row(connection, artifact_id))

    @staticmethod
    def _mark_types_stale(
        connection: sqlite3.Connection,
        project_id: str,
        scene_index: int,
        shot_index: int,
        artifact_types: tuple[ArtifactType, ...],
    ) -> list[str]:
        if not artifact_types:
            return []
        values = tuple(item.value for item in artifact_types)
        placeholders = ",".join("?" for _ in values)
        rows = connection.execute(
            f"""
            SELECT artifact_id FROM artifacts
            WHERE project_id = ? AND scene_index = ? AND shot_index = ?
              AND artifact_type IN ({placeholders}) AND status = ?
            """,
            (
                project_id,
                scene_index,
                shot_index,
                *values,
                ArtifactStatus.ACTIVE.value,
            ),
        ).fetchall()
        ids = [row["artifact_id"] for row in rows]
        if ids:
            id_placeholders = ",".join("?" for _ in ids)
            connection.execute(
                f"UPDATE artifacts SET status = ? WHERE artifact_id IN ({id_placeholders})",
                (ArtifactStatus.STALE.value, *ids),
            )
        return ids

    @staticmethod
    def _upsert_state(
        connection: sqlite3.Connection,
        project_id: str,
        scene_index: int,
        shot_index: int,
        readiness: ShotReadiness,
        input_hash: str,
        stale_reason: str | None,
        updated_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO shots(
                project_id, scene_index, shot_index, readiness,
                input_hash, stale_reason, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, scene_index, shot_index) DO UPDATE SET
                readiness = excluded.readiness,
                input_hash = excluded.input_hash,
                stale_reason = excluded.stale_reason,
                updated_at = excluded.updated_at
            """,
            (
                project_id,
                scene_index,
                shot_index,
                readiness.value,
                input_hash,
                stale_reason,
                updated_at,
            ),
        )

    @staticmethod
    def _row_to_version(
        connection: sqlite3.Connection, row: sqlite3.Row
    ) -> ArtifactVersion:
        input_rows = connection.execute(
            "SELECT input_name, input_hash FROM artifact_inputs WHERE artifact_id = ? ORDER BY input_name",
            (row["artifact_id"],),
        ).fetchall()
        return ArtifactVersion(
            artifact_id=row["artifact_id"],
            project_id=row["project_id"],
            scene_index=int(row["scene_index"]),
            shot_index=int(row["shot_index"]),
            artifact_type=ArtifactType(row["artifact_type"]),
            version=int(row["version"]),
            status=ArtifactStatus(row["status"]),
            input_hash=row["input_hash"],
            relative_path=row["relative_path"],
            inputs={item["input_name"]: item["input_hash"] for item in input_rows},
            metadata=json.loads(row["metadata_json"]),
            created_at=row["created_at"],
            activated_at=row["activated_at"],
        )


def _shot_key(project_id: str, scene_index: int, shot_index: int) -> tuple[str, int, int]:
    normalized_project = str(project_id or "").strip()
    if not normalized_project:
        raise ValueError("project_id cannot be empty")
    normalized_scene = int(scene_index)
    normalized_shot = int(shot_index)
    if normalized_scene < 0 or normalized_shot < 0:
        raise ValueError("scene_index and shot_index must be non-negative")
    return normalized_project, normalized_scene, normalized_shot


def _relative_path(value: str) -> str:
    normalized = str(value or "").replace("\\", "/").strip()
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise ValueError("relative_path must stay within the workspace")
    return path.as_posix()


def _readiness_after_generation(artifact_type: ArtifactType) -> ShotReadiness:
    return {
        ArtifactType.STORYBOARD: ShotReadiness.NEEDS_ASSETS,
        ArtifactType.KEYFRAME: ShotReadiness.READY,
        ArtifactType.VIDEO: ShotReadiness.REVIEW_REQUIRED,
    }[artifact_type]


def _get_shot_row(
    connection: sqlite3.Connection, project_id: str, scene_index: int, shot_index: int
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT * FROM shots
        WHERE project_id = ? AND scene_index = ? AND shot_index = ?
        """,
        (project_id, scene_index, shot_index),
    ).fetchone()
    if row is None:  # pragma: no cover - guarded by callers' insert/update
        raise RuntimeError("shot state disappeared during transaction")
    return row


def _get_artifact_row(connection: sqlite3.Connection, artifact_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
    ).fetchone()
    if row is None:  # pragma: no cover - guarded by callers' insert/update
        raise RuntimeError("artifact version disappeared during transaction")
    return row


def _row_to_shot_state(row: sqlite3.Row) -> ShotState:
    return ShotState(
        project_id=row["project_id"],
        scene_index=int(row["scene_index"]),
        shot_index=int(row["shot_index"]),
        readiness=ShotReadiness(row["readiness"]),
        input_hash=row["input_hash"],
        stale_reason=row["stale_reason"],
        updated_at=row["updated_at"],
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
