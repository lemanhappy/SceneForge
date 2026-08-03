from __future__ import annotations

from datetime import datetime
from typing import Protocol, Sequence, runtime_checkable

from domain.jobs import EnqueueResult, GenerationJob, JobSpec, JobState


@runtime_checkable
class JobQueue(Protocol):
    """Durable queue contract based only on serializable job descriptions."""

    def enqueue(self, spec: JobSpec) -> EnqueueResult: ...

    def claim(self, worker_id: str, job_types: Sequence[str] | None = None, *, lease_owner_id: str | None = None) -> GenerationJob | None: ...

    def get(self, job_id: str) -> GenerationJob | None: ...

    def list_recent(self, limit: int = 50, project_id: str | None = None) -> list[GenerationJob]: ...

    def list_remote_active(self, limit: int = 50) -> list[GenerationJob]: ...

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
    ) -> GenerationJob: ...

    def update_progress(self, job_id: str, current: int, total: int, *, lease_owner_id: str | None = None) -> GenerationJob: ...

    def append_event(
        self,
        job_id: str,
        stage: str,
        message: str,
        metadata: dict | None = None,
        *,
        lease_owner_id: str | None = None,
    ) -> GenerationJob: ...

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
    ) -> GenerationJob: ...

    def attach_project(self, job_id: str, project_id: str, *, lease_owner_id: str | None = None) -> GenerationJob: ...

    def request_cancel(self, job_id: str) -> GenerationJob: ...

    def recover_interrupted(self) -> int: ...

    def acquire_worker_lease(self, owner_id: str, *, lease_name: str = "local_generation_worker", ttl_seconds: float = 30.0) -> bool: ...

    def renew_worker_lease(self, owner_id: str, *, lease_name: str = "local_generation_worker", ttl_seconds: float = 30.0) -> bool: ...

    def release_worker_lease(self, owner_id: str, *, lease_name: str = "local_generation_worker") -> bool: ...

    def owns_worker_lease(self, owner_id: str, *, lease_name: str = "local_generation_worker") -> bool: ...
