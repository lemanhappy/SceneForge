from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol, runtime_checkable

from interfaces.video_output import VideoOutput


class RemoteVideoState(str, Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RemoteVideoInspection:
    state: RemoteVideoState
    status: str
    output: VideoOutput | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class RemoteVideoProvider(Protocol):
    async def inspect_remote_task(
        self,
        remote_task_id: str,
        *,
        model: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> RemoteVideoInspection: ...
