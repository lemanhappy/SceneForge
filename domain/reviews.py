from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class ReviewStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REVISED = "revised"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    CANCELED = "canceled"


@dataclass(slots=True)
class ReviewRecord:
    review_id: str
    project_id: str
    stage: str
    status: ReviewStatus = ReviewStatus.PENDING
    summary: str = ""
    artifact_version: str = "v1"
    artifact_refs: list[str] = field(default_factory=list)
    created_at: str = ""
    resolved_at: str | None = None

    @classmethod
    def from_mapping(cls, project_id: str, value: Mapping[str, Any]) -> "ReviewRecord":
        return cls(
            review_id=str(value.get("review_id") or ""),
            project_id=project_id,
            stage=str(value.get("stage") or ""),
            status=ReviewStatus(str(value.get("status") or ReviewStatus.PENDING.value)),
            summary=str(value.get("summary") or ""),
            artifact_version=str(value.get("artifact_version") or "v1"),
            artifact_refs=[str(item) for item in value.get("artifact_refs", []) or []],
            created_at=str(value.get("created_at") or ""),
            resolved_at=(str(value["resolved_at"]) if value.get("resolved_at") else None),
        )
