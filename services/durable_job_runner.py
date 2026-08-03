from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import replace
from typing import Any, Optional

from domain.jobs import EnqueueResult, GenerationJob, JobSpec, JobState
from infrastructure.sqlite.job_queue import SQLiteJobQueue

from .job_handlers import JobHandler, JobHandlerRegistry
from .local_worker import LocalWorker


_ACTIVE_STATES = {
    JobState.QUEUED,
    JobState.RUNNING,
    JobState.WAITING_PROVIDER,
    JobState.RETRY_WAIT,
    JobState.CANCEL_REQUESTED,
}


class DurableJobRunner:
    """Legacy JobRunner facade backed by SQLite and a recoverable local worker."""

    def __init__(
        self,
        queue: SQLiteJobQueue,
        max_concurrent: Optional[int] = None,
        *,
        remote_reconciler: Any = None,
    ) -> None:
        self.queue = queue
        self.registry = JobHandlerRegistry()
        self._callbacks: dict[str, Callable[[dict], None]] = {}
        self._callbacks_lock = threading.Lock()
        self._terminal_hook: Callable[[GenerationJob], None] | None = None
        self.worker = LocalWorker(
            queue,
            self.registry,
            concurrency=max_concurrent or 1,
            on_terminal=self._on_terminal,
            remote_reconciler=remote_reconciler,
        )

    def register_handler(self, job_type: str, handler: JobHandler) -> None:
        self.registry.register(job_type, handler)

    def has_handler(self, job_type: str) -> bool:
        return self.registry.has(job_type)

    def set_terminal_hook(self, hook: Callable[[GenerationJob], None] | None) -> None:
        self._terminal_hook = hook

    def start(self) -> None:
        self.worker.start()

    def stop(self, timeout: float = 5.0) -> None:
        self.worker.stop(timeout)

    def submit_job(
        self,
        spec: JobSpec,
        *,
        key: str | None = None,
        on_done: Callable[[dict], None] | None = None,
    ) -> dict:
        if key:
            active = self._active_for_key(key)
            if active is not None:
                return {"accepted": False, "state": "busy", "job_id": active.job_id, "key": key}
            spec = replace(spec, concurrency_key=key, idempotency_key=f"active:{key}")
        result: EnqueueResult = self.queue.enqueue(spec)
        if not result.accepted:
            return {
                "accepted": False,
                "state": "busy",
                "job_id": result.job.job_id,
                "key": key,
            }
        if on_done is not None:
            with self._callbacks_lock:
                self._callbacks[result.job.job_id] = on_done
        self.worker.wake()
        return self._legacy_record(result.job, accepted=True)

    def get(self, job_id: str) -> Optional[dict]:
        job = self.queue.get(job_id)
        return self._legacy_record(job) if job is not None else None

    def is_running(self, key: str) -> bool:
        return self._active_for_key(key) is not None

    def running_job_id(self, key: str) -> Optional[str]:
        job = self._active_for_key(key)
        return job.job_id if job is not None else None

    def last_job(self, key: str) -> Optional[dict]:
        jobs = self.queue.list_for_key(key, limit=1)
        return self._legacy_record(jobs[0]) if jobs else None

    def last_job_spec(self, key: str) -> JobSpec | None:
        """Return the latest stored specification for an explicit user retry."""
        jobs = self.queue.list_for_key(key, limit=1)
        return jobs[0].spec if jobs else None

    def list_recent(self, limit: int = 50) -> list[dict]:
        records: list[dict] = []
        for job in self.queue.list_recent(limit=limit):
            legacy = self._legacy_record(job)
            progress = legacy.get("progress") or []
            payload = dict(job.spec.payload or {})
            result = dict(job.result or {})
            project_id = (
                job.spec.project_id
                or payload.get("session_id")
                or result.get("session_id")
            )
            records.append(
                {
                    "job_id": job.job_id,
                    "key": job.spec.concurrency_key,
                    "project_id": project_id,
                    "job_type": job.spec.job_type,
                    "state": legacy["state"],
                    "internal_state": job.state.value,
                    "error": legacy.get("error"),
                    "steps": len(progress),
                    "last": progress[-1].get("message", "") if progress else "",
                    "created_at": job.created_at,
                    "updated_at": job.updated_at,
                }
            )
        return records

    def request_cancel(self, job_id: str) -> dict:
        return self._legacy_record(self.queue.request_cancel(job_id))

    def _active_for_key(self, key: str) -> GenerationJob | None:
        return next((job for job in self.queue.list_for_key(key) if job.state in _ACTIVE_STATES), None)

    def _on_terminal(self, job: GenerationJob) -> None:
        legacy = self._legacy_record(job)
        with self._callbacks_lock:
            callback = self._callbacks.pop(job.job_id, None)
        if callback is not None:
            try:
                callback(legacy)
            except Exception:
                pass
        if self._terminal_hook is not None:
            try:
                self._terminal_hook(job)
            except Exception:
                pass

    @staticmethod
    def _legacy_record(job: GenerationJob, *, accepted: bool = True) -> dict:
        if job.state in _ACTIVE_STATES:
            state = "running"
            error = None
        elif job.state is JobState.SUCCEEDED:
            state = "done"
            error = None
        elif job.state is JobState.INTERRUPTED:
            state = "failed"
            error = "Interrupted: application exited before the job completed"
        elif job.state is JobState.CANCELED:
            state = "failed"
            error = "Canceled"
        else:
            state = "failed"
            detail = job.error_message or job.error_code or "job failed"
            error = f"{job.error_code}: {detail}" if job.error_code and job.error_message else detail

        progress: list[dict] = []
        for event in job.events:
            item = {"stage": event.get("stage", ""), "message": event.get("message", "")}
            metadata = dict(event.get("meta") or {})
            metadata.pop("task_id", None)
            metadata.pop("job_id", None)
            metadata.pop("polling_url", None)
            metadata.pop("artifact_path", None)
            metadata.pop("base_url", None)
            if metadata:
                item["meta"] = metadata
            progress.append(item)
        return {
            "job_id": job.job_id,
            "job_type": job.spec.job_type,
            "key": job.spec.concurrency_key,
            "state": state,
            "internal_state": job.state.value,
            "result": job.result,
            "error": error,
            "accepted": accepted,
            "progress": progress,
        }
