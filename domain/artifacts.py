from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class ArtifactType(str, Enum):
    STORYBOARD = "storyboard"
    KEYFRAME = "keyframe"
    VIDEO = "video"


class ArtifactStatus(str, Enum):
    ACTIVE = "active"
    STALE = "stale"
    ARCHIVED = "archived"


class ShotReadiness(str, Enum):
    DRAFT = "draft"
    NEEDS_ASSETS = "needs_assets"
    NEEDS_PROMPT_REVIEW = "needs_prompt_review"
    READY = "ready"
    GENERATING = "generating"
    REVIEW_REQUIRED = "review_required"
    APPROVED = "approved"
    STALE = "stale"
    FAILED = "failed"


_ARTIFACT_ORDER = (
    ArtifactType.STORYBOARD,
    ArtifactType.KEYFRAME,
    ArtifactType.VIDEO,
)


def affected_artifact_types(
    changed_type: ArtifactType | str,
    *,
    include_changed: bool = True,
) -> tuple[ArtifactType, ...]:
    normalized = ArtifactType(changed_type)
    start = _ARTIFACT_ORDER.index(normalized) + (0 if include_changed else 1)
    return _ARTIFACT_ORDER[start:]


def compute_input_hash(value: Any) -> str:
    """Return a stable hash for JSON-shaped generation inputs."""
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise TypeError(f"cannot hash value of type {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class ShotState:
    project_id: str
    scene_index: int
    shot_index: int
    readiness: ShotReadiness = ShotReadiness.DRAFT
    input_hash: str = ""
    stale_reason: str | None = None
    updated_at: str = ""


@dataclass(frozen=True, slots=True)
class ArtifactVersion:
    artifact_id: str
    project_id: str
    scene_index: int
    shot_index: int
    artifact_type: ArtifactType
    version: int
    status: ArtifactStatus
    input_hash: str
    relative_path: str
    inputs: Mapping[str, str] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = ""
    activated_at: str | None = None
