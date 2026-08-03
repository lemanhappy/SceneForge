from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Sequence
from uuid import uuid4

from domain.jobs import (
    EnqueueResult,
    GenerationJob,
    JobSpec,
    JobState,
    TERMINAL_JOB_STATES,
    ensure_job_transition,
)

from .database import SQLiteDatabase


_ACTIVE_IDEMPOTENT_STATES = (
    JobState.QUEUED.value,
    JobState.RUNNING.value,
    JobState.WAITING_PROVIDER.value,
    JobState.RETRY_WAIT.value,
    JobState.CANCEL_REQUESTED.value,
)


class WorkerLeaseLostError(RuntimeError):
    """Raised when a stale worker tries to mutate the durable queue."""


class SQLiteJobQueue:
    """SQLite-backed durable queue; execution remains the worker's concern."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database
        self.database.migrate()

    def enqueue(self, spec: JobSpec) -> EnqueueResult:
        now = _utc_now()
        job_id = f"job_{uuid4().hex}"
        payload_json = _dump_json(dict(spec.payload))
        try:
            with self.database.transaction(immediate=True) as connection:
                connection.execute(
                    """
                    INSERT INTO generation_jobs(
                        job_id, project_id, job_type, entity_type, entity_id,
                        concurrency_key, idempotency_key, state, priority, provider, model,
                        attempt, max_attempts, request_payload_json,
                        estimated_cost, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        spec.project_id,
                        spec.job_type,
                        spec.entity_type,
                        spec.entity_id,
                        spec.concurrency_key,
                        spec.idempotency_key,
                        JobState.QUEUED.value,
                        spec.priority,
                        spec.provider,
                        spec.model,
                        spec.max_attempts,
                        payload_json,
                        spec.estimated_cost,
                        now,
                        now,
                    ),
                )
                row = self._get_row(connection, job_id)
        except sqlite3.IntegrityError:
            if not spec.idempotency_key:
                raise
            existing = self._find_active_by_idempotency_key(spec.idempotency_key)
            if existing is None:
                raise
            return EnqueueResult(accepted=False, job=existing, reason="duplicate")
        return EnqueueResult(accepted=True, job=self._row_to_job(row))

    def claim(
        self,
        worker_id: str,
        job_types: Sequence[str] | None = None,
        *,
        lease_owner_id: str | None = None,
    ) -> GenerationJob | None:
        if not worker_id.strip():
            raise ValueError("worker_id cannot be empty")
        now = _utc_now()
        params: list[Any] = [JobState.QUEUED.value, JobState.RETRY_WAIT.value, now]
        type_clause = ""
        if job_types:
            normalized = [str(item).strip() for item in job_types if str(item).strip()]
            if not normalized:
                return None
            type_clause = f" AND job_type IN ({','.join('?' for _ in normalized)})"
            params.extend(normalized)
        query = (
            "SELECT * FROM generation_jobs "
            "WHERE state IN (?, ?) AND attempt < max_attempts "
            "AND (next_attempt_at IS NULL OR next_attempt_at <= ?)"
            f"{type_clause} ORDER BY priority DESC, created_at ASC LIMIT 1"
        )
        with self.database.transaction(immediate=True) as connection:
            self._assert_worker_lease(connection, lease_owner_id)
            row = connection.execute(query, params).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE generation_jobs
                SET state = ?, worker_id = ?, attempt = attempt + 1,
                    started_at = COALESCE(started_at, ?), updated_at = ?
                WHERE job_id = ? AND state IN (?, ?)
                """,
                (
                    JobState.RUNNING.value,
                    worker_id,
                    now,
                    now,
                    row["job_id"],
                    JobState.QUEUED.value,
                    JobState.RETRY_WAIT.value,
                ),
            )
            claimed = self._get_row(connection, row["job_id"])
        return self._row_to_job(claimed)

    def get(self, job_id: str) -> GenerationJob | None:
        with self.database.connection() as connection:
            row = connection.execute("SELECT * FROM generation_jobs WHERE job_id = ?", (job_id,)).fetchone()
            events = self._events(connection, job_id) if row is not None else []
        return self._row_to_job(row, events) if row is not None else None

    def list_recent(self, limit: int = 50, project_id: str | None = None) -> list[GenerationJob]:
        safe_limit = max(1, min(int(limit), 500))
        with self.database.connection() as connection:
            if project_id is None:
                rows = connection.execute(
                    "SELECT * FROM generation_jobs ORDER BY created_at DESC LIMIT ?", (safe_limit,)
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM generation_jobs WHERE project_id = ? ORDER BY created_at DESC LIMIT ?",
                    (project_id, safe_limit),
                ).fetchall()
            events_by_job = {row["job_id"]: self._events(connection, row["job_id"]) for row in rows}
        return [self._row_to_job(row, events_by_job[row["job_id"]]) for row in rows]

    def list_for_key(self, concurrency_key: str, limit: int = 50) -> list[GenerationJob]:
        safe_limit = max(1, min(int(limit), 500))
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM generation_jobs
                WHERE concurrency_key = ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (concurrency_key, safe_limit),
            ).fetchall()
            events_by_job = {row["job_id"]: self._events(connection, row["job_id"]) for row in rows}
        return [self._row_to_job(row, events_by_job[row["job_id"]]) for row in rows]

    def list_remote_active(self, limit: int = 50) -> list[GenerationJob]:
        safe_limit = max(1, min(int(limit), 500))
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM generation_jobs
                WHERE state IN (?, ?)
                  AND remote_task_id IS NOT NULL
                  AND remote_provider IS NOT NULL
                ORDER BY updated_at ASC LIMIT ?
                """,
                (
                    JobState.WAITING_PROVIDER.value,
                    JobState.CANCEL_REQUESTED.value,
                    safe_limit,
                ),
            ).fetchall()
            events_by_job = {row["job_id"]: self._events(connection, row["job_id"]) for row in rows}
        return [self._row_to_job(row, events_by_job[row["job_id"]]) for row in rows]

    def transition(
        self,
        job_id: str,
        target: JobState,
        *,
        result: dict | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        remote_task_id: str | None = None,
        next_attempt_at: datetime | None = None,
        actual_cost: float | None = None,
        lease_owner_id: str | None = None,
    ) -> GenerationJob:
        target = JobState(target)
        now = _utc_now()
        with self.database.transaction(immediate=True) as connection:
            self._assert_worker_lease(connection, lease_owner_id)
            row = self._get_row(connection, job_id)
            current = JobState(row["state"])
            ensure_job_transition(current, target)
            if target in {JobState.RUNNING, JobState.RETRY_WAIT} and int(row["attempt"]) >= int(row["max_attempts"]):
                raise ValueError(f"job {job_id} has exhausted its {row['max_attempts']} attempts")
            started_at = row["started_at"]
            attempt = int(row["attempt"])
            if target is JobState.RUNNING:
                started_at = started_at or now
                attempt += 1
            finished_at = now if target in TERMINAL_JOB_STATES else row["finished_at"]
            cancel_requested_at = now if target is JobState.CANCEL_REQUESTED else row["cancel_requested_at"]
            connection.execute(
                """
                UPDATE generation_jobs
                SET state = ?, attempt = ?, result_json = ?, error_code = ?, error_message = ?,
                    remote_task_id = COALESCE(?, remote_task_id), next_attempt_at = ?,
                    actual_cost = COALESCE(?, actual_cost), cancel_requested_at = ?,
                    started_at = ?, finished_at = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (
                    target.value,
                    attempt,
                    _dump_json(result) if result is not None else row["result_json"],
                    error_code,
                    error_message,
                    remote_task_id,
                    _format_datetime(next_attempt_at),
                    actual_cost,
                    cancel_requested_at,
                    started_at,
                    finished_at,
                    now,
                    job_id,
                ),
            )
            updated = self._get_row(connection, job_id)
        return self._row_to_job(updated)

    def update_progress(
        self,
        job_id: str,
        current: int,
        total: int,
        *,
        lease_owner_id: str | None = None,
    ) -> GenerationJob:
        current_value, total_value = int(current), int(total)
        if current_value < 0 or total_value < 0 or (total_value and current_value > total_value):
            raise ValueError("invalid progress values")
        with self.database.transaction(immediate=True) as connection:
            self._assert_worker_lease(connection, lease_owner_id)
            row = self._get_row(connection, job_id)
            if JobState(row["state"]) in TERMINAL_JOB_STATES:
                raise ValueError("cannot update progress for a terminal job")
            connection.execute(
                """
                UPDATE generation_jobs
                SET progress_current = ?, progress_total = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (current_value, total_value, _utc_now(), job_id),
            )
            updated = self._get_row(connection, job_id)
        return self._row_to_job(updated)

    def append_event(
        self,
        job_id: str,
        stage: str,
        message: str,
        metadata: dict | None = None,
        *,
        lease_owner_id: str | None = None,
    ) -> GenerationJob:
        stage_text = str(stage).strip()
        message_text = str(message).strip()
        if not stage_text or not message_text:
            raise ValueError("job event stage and message cannot be empty")
        metadata_json = _dump_json(metadata or {})
        with self.database.transaction(immediate=True) as connection:
            self._assert_worker_lease(connection, lease_owner_id)
            row = self._get_row(connection, job_id)
            connection.execute(
                """
                INSERT INTO job_events(job_id, stage, message, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (job_id, stage_text, message_text, metadata_json, _utc_now()),
            )
            connection.execute(
                """
                DELETE FROM job_events
                WHERE job_id = ? AND event_id NOT IN (
                    SELECT event_id FROM job_events
                    WHERE job_id = ? ORDER BY event_id DESC LIMIT 100
                )
                """,
                (job_id, job_id),
            )
            events = self._events(connection, job_id)
        return self._row_to_job(row, events)

    def bind_remote_task(
        self,
        job_id: str,
        remote_task_id: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        artifact_path: str | None = None,
        metadata: dict | None = None,
        lease_owner_id: str | None = None,
    ) -> GenerationJob:
        remote_id = str(remote_task_id).strip()
        if not remote_id:
            raise ValueError("remote_task_id cannot be empty")
        with self.database.transaction(immediate=True) as connection:
            self._assert_worker_lease(connection, lease_owner_id)
            row = self._get_row(connection, job_id)
            state = JobState(row["state"])
            if state is JobState.RUNNING:
                target = JobState.WAITING_PROVIDER.value
            elif state is JobState.WAITING_PROVIDER:
                target = state.value
            else:
                raise ValueError(f"cannot bind a remote task while job is {state.value}")
            connection.execute(
                """
                UPDATE generation_jobs
                SET state = ?, remote_task_id = ?,
                    remote_provider = COALESCE(?, remote_provider),
                    model = COALESCE(?, model),
                    remote_artifact_path = COALESCE(?, remote_artifact_path),
                    remote_metadata_json = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (
                    target,
                    remote_id,
                    (str(provider).strip() if provider else None),
                    (str(model).strip() if model else None),
                    (str(artifact_path).strip() if artifact_path else None),
                    _dump_json(metadata or {}),
                    _utc_now(),
                    job_id,
                ),
            )
            updated = self._get_row(connection, job_id)
            events = self._events(connection, job_id)
        return self._row_to_job(updated, events)

    def attach_project(
        self,
        job_id: str,
        project_id: str,
        *,
        lease_owner_id: str | None = None,
    ) -> GenerationJob:
        identifier = str(project_id).strip()
        if not identifier:
            raise ValueError("project_id cannot be empty")
        with self.database.transaction(immediate=True) as connection:
            self._assert_worker_lease(connection, lease_owner_id)
            self._get_row(connection, job_id)
            connection.execute(
                """
                UPDATE generation_jobs
                SET project_id = ?, concurrency_key = COALESCE(concurrency_key, ?), updated_at = ?
                WHERE job_id = ?
                """,
                (identifier, identifier, _utc_now(), job_id),
            )
            updated = self._get_row(connection, job_id)
            events = self._events(connection, job_id)
        return self._row_to_job(updated, events)

    def request_cancel(self, job_id: str) -> GenerationJob:
        now = _utc_now()
        with self.database.transaction(immediate=True) as connection:
            row = self._get_row(connection, job_id)
            current = JobState(row["state"])
            if current in TERMINAL_JOB_STATES or current is JobState.CANCEL_REQUESTED:
                events = self._events(connection, job_id)
                return self._row_to_job(row, events)
            target = (
                JobState.CANCELED
                if current in {JobState.QUEUED, JobState.RETRY_WAIT, JobState.INTERRUPTED}
                else JobState.CANCEL_REQUESTED
            )
            ensure_job_transition(current, target)
            connection.execute(
                """
                UPDATE generation_jobs
                SET state = ?, cancel_requested_at = ?, finished_at = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (
                    target.value,
                    now,
                    now if target is JobState.CANCELED else row["finished_at"],
                    now,
                    job_id,
                ),
            )
            updated = self._get_row(connection, job_id)
            events = self._events(connection, job_id)
        return self._row_to_job(updated, events)

    def recover_interrupted(self) -> int:
        now = _utc_now()
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE generation_jobs
                SET state = CASE
                        WHEN state = ?
                         AND remote_task_id IS NOT NULL
                         AND remote_provider IS NOT NULL THEN ?
                        WHEN state = ? THEN ?
                        ELSE ?
                    END,
                    worker_id = NULL,
                    finished_at = CASE
                        WHEN state = ?
                         AND (remote_task_id IS NULL OR remote_provider IS NULL) THEN ?
                        ELSE finished_at
                    END,
                    updated_at = ?
                WHERE state IN (?, ?)
                   OR (
                       state = ?
                       AND (remote_task_id IS NULL OR remote_provider IS NULL)
                   )
                """,
                (
                    JobState.CANCEL_REQUESTED.value,
                    JobState.CANCEL_REQUESTED.value,
                    JobState.CANCEL_REQUESTED.value,
                    JobState.CANCELED.value,
                    JobState.INTERRUPTED.value,
                    JobState.CANCEL_REQUESTED.value,
                    now,
                    now,
                    JobState.RUNNING.value,
                    JobState.CANCEL_REQUESTED.value,
                    JobState.WAITING_PROVIDER.value,
                ),
            )
            return int(cursor.rowcount)

    def acquire_worker_lease(
        self,
        owner_id: str,
        *,
        lease_name: str = "local_generation_worker",
        ttl_seconds: float = 30.0,
    ) -> bool:
        owner = str(owner_id).strip()
        if not owner:
            raise ValueError("owner_id cannot be empty")
        now = time.time()
        expires_at = now + max(0.5, float(ttl_seconds))
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT owner_id, expires_at FROM worker_leases WHERE lease_name = ?",
                (lease_name,),
            ).fetchone()
            if row is not None and row["owner_id"] != owner and float(row["expires_at"]) > now:
                return False
            connection.execute(
                """
                INSERT INTO worker_leases(lease_name, owner_id, heartbeat_at, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(lease_name) DO UPDATE SET
                    owner_id = excluded.owner_id,
                    heartbeat_at = excluded.heartbeat_at,
                    expires_at = excluded.expires_at
                """,
                (lease_name, owner, now, expires_at),
            )
        return True

    def renew_worker_lease(
        self,
        owner_id: str,
        *,
        lease_name: str = "local_generation_worker",
        ttl_seconds: float = 30.0,
    ) -> bool:
        now = time.time()
        expires_at = now + max(0.5, float(ttl_seconds))
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE worker_leases
                SET heartbeat_at = ?, expires_at = ?
                WHERE lease_name = ? AND owner_id = ?
                """,
                (now, expires_at, lease_name, owner_id),
            )
            return cursor.rowcount == 1

    def release_worker_lease(
        self,
        owner_id: str,
        *,
        lease_name: str = "local_generation_worker",
    ) -> bool:
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "DELETE FROM worker_leases WHERE lease_name = ? AND owner_id = ?",
                (lease_name, owner_id),
            )
            return cursor.rowcount == 1

    def owns_worker_lease(
        self,
        owner_id: str,
        *,
        lease_name: str = "local_generation_worker",
    ) -> bool:
        now = time.time()
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM worker_leases
                WHERE lease_name = ? AND owner_id = ? AND expires_at > ?
                """,
                (lease_name, owner_id, now),
            ).fetchone()
        return row is not None

    @staticmethod
    def _assert_worker_lease(
        connection: sqlite3.Connection,
        owner_id: str | None,
        *,
        lease_name: str = "local_generation_worker",
    ) -> None:
        if owner_id is None:
            return
        row = connection.execute(
            """
            SELECT 1 FROM worker_leases
            WHERE lease_name = ? AND owner_id = ? AND expires_at > ?
            """,
            (lease_name, owner_id, time.time()),
        ).fetchone()
        if row is None:
            raise WorkerLeaseLostError(f"worker lease is no longer owned by {owner_id}")

    def _find_active_by_idempotency_key(self, key: str) -> GenerationJob | None:
        placeholders = ",".join("?" for _ in _ACTIVE_IDEMPOTENT_STATES)
        with self.database.connection() as connection:
            row = connection.execute(
                f"SELECT * FROM generation_jobs WHERE idempotency_key = ? AND state IN ({placeholders}) "
                "ORDER BY created_at DESC LIMIT 1",
                (key, *_ACTIVE_IDEMPOTENT_STATES),
            ).fetchone()
        return self._row_to_job(row) if row is not None else None

    @staticmethod
    def _get_row(connection: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM generation_jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown job_id: {job_id}")
        return row

    @staticmethod
    def _row_to_job(row: sqlite3.Row, events: list[dict[str, Any]] | None = None) -> GenerationJob:
        spec = JobSpec(
            job_type=row["job_type"],
            payload=json.loads(row["request_payload_json"]),
            project_id=row["project_id"],
            entity_type=row["entity_type"],
            entity_id=row["entity_id"],
            concurrency_key=row["concurrency_key"],
            idempotency_key=row["idempotency_key"],
            priority=int(row["priority"]),
            provider=row["provider"],
            model=row["model"],
            max_attempts=int(row["max_attempts"]),
            estimated_cost=row["estimated_cost"],
        )
        return GenerationJob(
            job_id=row["job_id"],
            spec=spec,
            state=JobState(row["state"]),
            attempt=int(row["attempt"]),
            progress_current=int(row["progress_current"]),
            progress_total=int(row["progress_total"]),
            remote_task_id=row["remote_task_id"],
            remote_provider=row["remote_provider"],
            remote_metadata=(json.loads(row["remote_metadata_json"]) if row["remote_metadata_json"] else {}),
            remote_artifact_path=row["remote_artifact_path"],
            worker_id=row["worker_id"],
            result=(json.loads(row["result_json"]) if row["result_json"] else None),
            error_code=row["error_code"],
            error_message=row["error_message"],
            actual_cost=row["actual_cost"],
            next_attempt_at=row["next_attempt_at"],
            cancel_requested_at=row["cancel_requested_at"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            updated_at=row["updated_at"],
            events=list(events or []),
        )

    @staticmethod
    def _events(connection: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT stage, message, metadata_json, created_at FROM job_events WHERE job_id = ? ORDER BY event_id",
            (job_id,),
        ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            event = {"stage": row["stage"], "message": row["message"], "created_at": row["created_at"]}
            metadata = json.loads(row["metadata_json"])
            if metadata:
                event["meta"] = metadata
            events.append(event)
        return events


def _dump_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("job payload and result must be JSON serializable") from exc


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _format_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
