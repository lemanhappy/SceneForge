from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from repositories.session_state_store import SessionStateStore

from .database import SQLiteDatabase


_STORE_LOCKS: dict[str, threading.RLock] = {}
_STORE_LOCKS_GUARD = threading.Lock()


def _store_lock(path: str) -> threading.RLock:
    with _STORE_LOCKS_GUARD:
        lock = _STORE_LOCKS.get(path)
        if lock is None:
            lock = threading.RLock()
            _STORE_LOCKS[path] = lock
        return lock


class SQLiteSessionStateStore(SessionStateStore):
    """Transactional backing store for the current SessionIndex facade."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database
        self.database.migrate()
        self._local = threading.local()
        self._process_lock = _store_lock(str(self.database.path))

    @contextmanager
    def locked(self) -> Iterator[None]:
        existing = getattr(self._local, "connection", None)
        if existing is not None:
            yield
            return
        with self._process_lock:
            with self.database.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._local.connection = connection
                try:
                    yield
                except BaseException:
                    connection.rollback()
                    raise
                else:
                    connection.commit()
                finally:
                    self._local.connection = None

    def load(self) -> dict[str, Any]:
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            return self._load(connection)
        with self.database.connection() as connection:
            connection.execute("BEGIN")
            try:
                result = self._load(connection)
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()
                return result

    def save(self, data: dict[str, Any]) -> None:
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            self._save(connection, data)
            return
        with self.locked():
            self._save(self._local.connection, data)

    def has_projects(self) -> bool:
        with self.database.connection() as connection:
            row = connection.execute("SELECT 1 FROM projects LIMIT 1").fetchone()
        return row is not None

    @staticmethod
    def _load(connection: sqlite3.Connection) -> dict[str, Any]:
        sessions: dict[str, dict[str, Any]] = {}
        project_rows = connection.execute(
            "SELECT project_id, record_json FROM projects ORDER BY updated_at DESC, project_id"
        ).fetchall()
        for row in project_rows:
            try:
                record = json.loads(row["record_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError(f"project {row['project_id']!r} contains invalid record_json") from exc
            if not isinstance(record, dict):
                raise ValueError(f"project {row['project_id']!r} record_json must be an object")
            record["session_id"] = row["project_id"]
            record["review_tasks"] = []
            sessions[row["project_id"]] = record

        for row in connection.execute(
            "SELECT * FROM reviews ORDER BY project_id, created_at, review_id"
        ).fetchall():
            record = sessions.get(row["project_id"])
            if record is None:
                continue
            try:
                artifact_refs = json.loads(row["artifact_refs_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError(f"review {row['review_id']!r} contains invalid artifact_refs_json") from exc
            record["review_tasks"].append(
                {
                    "review_id": row["review_id"],
                    "session_id": row["project_id"],
                    "stage": row["stage"],
                    "status": row["status"],
                    "summary": row["summary"],
                    "artifact_version": row["artifact_version"],
                    "artifact_refs": artifact_refs,
                    "created_at": row["created_at"],
                    "resolved_at": row["resolved_at"],
                }
            )

        active_row = connection.execute(
            "SELECT value_json FROM app_state WHERE key = 'active_session_id'"
        ).fetchone()
        active_session_id = ""
        if active_row is not None:
            try:
                value = json.loads(active_row["value_json"])
                active_session_id = str(value or "")
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("active_session_id contains invalid JSON") from exc
        return {"active_session_id": active_session_id, "sessions": sessions}

    @staticmethod
    def _save(connection: sqlite3.Connection, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            raise ValueError("session state must be an object")
        sessions = data.get("sessions", {})
        if not isinstance(sessions, dict):
            raise ValueError("sessions must be an object")
        active_session_id = str(data.get("active_session_id") or "")
        if active_session_id and active_session_id not in sessions:
            raise ValueError("active_session_id must reference an existing session")

        normalized: dict[str, dict[str, Any]] = {}
        for session_id, value in sessions.items():
            identifier = str(session_id).strip()
            if not identifier or not isinstance(value, dict):
                raise ValueError("every session must have a non-empty id and object record")
            record = dict(value)
            record["session_id"] = identifier
            if not str(record.get("working_dir") or "").strip():
                raise ValueError(f"session {identifier!r} is missing working_dir")
            review_tasks = record.get("review_tasks", []) or []
            if not isinstance(review_tasks, list) or any(not isinstance(item, dict) for item in review_tasks):
                raise ValueError(f"session {identifier!r} has invalid review_tasks")
            normalized[identifier] = record

        existing = {
            row["project_id"] for row in connection.execute("SELECT project_id FROM projects").fetchall()
        }
        removed = existing - set(normalized)
        if removed:
            placeholders = ",".join("?" for _ in removed)
            connection.execute(f"DELETE FROM projects WHERE project_id IN ({placeholders})", tuple(removed))

        now = _utc_now()
        for session_id, record in normalized.items():
            created_at = str(record.get("created_at") or now)
            updated_at = str(record.get("updated_at") or created_at)
            record_json = _dump_json(record)
            connection.execute(
                """
                INSERT INTO projects(
                    project_id, legacy_session_id, working_dir, mode, title,
                    stage, revision, record_json, created_at, updated_at,
                    series_id, episode_number, episode_title, previous_episode_id
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    legacy_session_id = excluded.legacy_session_id,
                    working_dir = excluded.working_dir,
                    mode = excluded.mode,
                    title = excluded.title,
                    stage = excluded.stage,
                    record_json = excluded.record_json,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    series_id = excluded.series_id,
                    episode_number = excluded.episode_number,
                    episode_title = excluded.episode_title,
                    previous_episode_id = excluded.previous_episode_id,
                    revision = projects.revision + 1
                WHERE projects.record_json <> excluded.record_json
                   OR projects.working_dir <> excluded.working_dir
                   OR projects.stage <> excluded.stage
                   OR projects.series_id IS NOT excluded.series_id
                   OR projects.episode_number IS NOT excluded.episode_number
                   OR projects.episode_title <> excluded.episode_title
                   OR projects.previous_episode_id IS NOT excluded.previous_episode_id
                   OR projects.updated_at <> excluded.updated_at
                """,
                (
                    session_id,
                    session_id,
                    str(record["working_dir"]),
                    str(record.get("mode") or "idea"),
                    str(record.get("idea") or record.get("user_requirement") or "")[:200],
                    str(record.get("stage") or "created"),
                    record_json,
                    created_at,
                    updated_at,
                    (str(record.get("series_id")) if record.get("series_id") else None),
                    (int(record["episode_number"]) if record.get("episode_number") is not None else None),
                    str(record.get("episode_title") or ""),
                    (str(record.get("previous_episode_id")) if record.get("previous_episode_id") else None),
                ),
            )
            connection.execute("DELETE FROM reviews WHERE project_id = ?", (session_id,))
            seen_reviews: set[str] = set()
            for ordinal, review in enumerate(record.get("review_tasks", []) or [], start=1):
                review_id = str(review.get("review_id") or f"review_{ordinal}")
                if review_id in seen_reviews:
                    raise ValueError(f"session {session_id!r} contains duplicate review_id {review_id!r}")
                seen_reviews.add(review_id)
                connection.execute(
                    """
                    INSERT INTO reviews(
                        project_id, review_id, stage, status, summary,
                        artifact_version, artifact_refs_json, created_at, resolved_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        review_id,
                        str(review.get("stage") or ""),
                        str(review.get("status") or "pending"),
                        str(review.get("summary") or ""),
                        str(review.get("artifact_version") or "v1"),
                        _dump_json(review.get("artifact_refs", []) or []),
                        str(review.get("created_at") or created_at),
                        (str(review["resolved_at"]) if review.get("resolved_at") else None),
                    ),
                )

        connection.execute(
            """
            INSERT INTO app_state(key, value_json, updated_at)
            VALUES ('active_session_id', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at
            """,
            (_dump_json(active_session_id), now),
        )


def _dump_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("session state must be JSON serializable") from exc


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
