from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_PROVIDER = "waiting_provider"
    SUCCEEDED = "succeeded"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELED = "canceled"
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


TERMINAL_JOB_STATES = frozenset({JobState.SUCCEEDED, JobState.CANCELED, JobState.FAILED})

ALLOWED_JOB_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.RUNNING, JobState.CANCEL_REQUESTED, JobState.CANCELED}),
    JobState.RUNNING: frozenset(
        {
            JobState.WAITING_PROVIDER,
            JobState.SUCCEEDED,
            JobState.CANCEL_REQUESTED,
            JobState.RETRY_WAIT,
            JobState.FAILED,
            JobState.INTERRUPTED,
        }
    ),
    JobState.WAITING_PROVIDER: frozenset(
        {
            JobState.RUNNING,
            JobState.SUCCEEDED,
            JobState.CANCEL_REQUESTED,
            JobState.RETRY_WAIT,
            JobState.FAILED,
            JobState.INTERRUPTED,
        }
    ),
    JobState.RETRY_WAIT: frozenset(
        {JobState.RUNNING, JobState.CANCEL_REQUESTED, JobState.CANCELED, JobState.FAILED}
    ),
    JobState.CANCEL_REQUESTED: frozenset({JobState.CANCELED, JobState.SUCCEEDED, JobState.FAILED}),
    JobState.INTERRUPTED: frozenset(
        {JobState.RUNNING, JobState.RETRY_WAIT, JobState.CANCEL_REQUESTED, JobState.CANCELED, JobState.FAILED}
    ),
    JobState.SUCCEEDED: frozenset(),
    JobState.CANCELED: frozenset(),
    JobState.FAILED: frozenset(),
}


class JobTransitionError(ValueError):
    pass


def ensure_job_transition(current: JobState, target: JobState) -> None:
    if target not in ALLOWED_JOB_TRANSITIONS[current]:
        raise JobTransitionError(f"illegal job transition: {current.value} -> {target.value}")


@dataclass(frozen=True, slots=True)
class JobSpec:
    """Serializable work description consumed by a registered worker handler."""

    job_type: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    project_id: str | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    concurrency_key: str | None = None
    idempotency_key: str | None = None
    priority: int = 0
    provider: str | None = None
    model: str | None = None
    max_attempts: int = 3
    estimated_cost: float | None = None

    def __post_init__(self) -> None:
        if not self.job_type.strip():
            raise ValueError("job_type cannot be empty")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")


@dataclass(slots=True)
class GenerationJob:
    job_id: str
    spec: JobSpec
    state: JobState
    attempt: int = 0
    progress_current: int = 0
    progress_total: int = 0
    remote_task_id: str | None = None
    remote_provider: str | None = None
    remote_metadata: dict[str, Any] = field(default_factory=dict)
    remote_artifact_path: str | None = None
    worker_id: str | None = None
    result: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    actual_cost: float | None = None
    next_attempt_at: str | None = None
    cancel_requested_at: str | None = None
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    updated_at: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    accepted: bool
    job: GenerationJob
    reason: str = ""
