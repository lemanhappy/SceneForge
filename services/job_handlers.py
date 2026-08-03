from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from domain.jobs import GenerationJob, JobSpec, JobState
from repositories.job_queue import JobQueue


class JobHandler(Protocol):
    def __call__(self, spec: JobSpec, context: "JobContext") -> Awaitable[dict[str, Any]]: ...


@dataclass(slots=True)
class JobContext:
    """Durable worker callbacks kept separate from UI progress events."""

    queue: JobQueue
    job_id: str
    lease_owner_id: str | None = None

    def progress(self, current: int, total: int) -> GenerationJob:
        return self.queue.update_progress(
            self.job_id,
            current,
            total,
            lease_owner_id=self.lease_owner_id,
        )

    def bind_remote_task(
        self,
        remote_task_id: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        artifact_path: str | None = None,
        metadata: dict | None = None,
    ) -> GenerationJob:
        if not str(remote_task_id).strip():
            raise ValueError("remote_task_id cannot be empty")
        return self.queue.bind_remote_task(
            self.job_id,
            str(remote_task_id).strip(),
            provider=provider,
            model=model,
            artifact_path=artifact_path,
            metadata=metadata,
            lease_owner_id=self.lease_owner_id,
        )

    def event(self, stage: str, message: str, metadata: dict | None = None) -> GenerationJob:
        values = dict(metadata or {})
        remote_task_id = values.get("task_id") or values.get("job_id")
        if remote_task_id:
            try:
                self.bind_remote_task(
                    str(remote_task_id),
                    provider=(str(values["provider"]) if values.get("provider") else None),
                    model=(str(values["model"]) if values.get("model") else None),
                    artifact_path=(str(values["artifact_path"]) if values.get("artifact_path") else None),
                    metadata=values,
                )
            except ValueError:
                pass
        return self.queue.append_event(
            self.job_id,
            stage,
            message,
            values,
            lease_owner_id=self.lease_owner_id,
        )

    def cancel_requested(self) -> bool:
        job = self.queue.get(self.job_id)
        return job is not None and job.state is JobState.CANCEL_REQUESTED


class JobHandlerRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, JobHandler] = {}

    def register(self, job_type: str, handler: JobHandler) -> None:
        normalized = str(job_type).strip()
        if not normalized:
            raise ValueError("job_type cannot be empty")
        if normalized in self._handlers:
            raise ValueError(f"handler already registered for job_type: {normalized}")
        self._handlers[normalized] = handler

    def get(self, job_type: str) -> JobHandler:
        try:
            return self._handlers[job_type]
        except KeyError as exc:
            raise KeyError(f"no handler registered for job_type: {job_type}") from exc

    def has(self, job_type: str) -> bool:
        return str(job_type).strip() in self._handlers

    async def dispatch(self, spec: JobSpec, context: JobContext) -> dict[str, Any]:
        return await self.get(spec.job_type)(spec, context)
