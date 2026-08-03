"""Framework-independent domain records used by SceneForge services."""

from .artifacts import (
    ArtifactStatus,
    ArtifactType,
    ArtifactVersion,
    ShotReadiness,
    ShotState,
    affected_artifact_types,
    compute_input_hash,
)
from .jobs import EnqueueResult, GenerationJob, JobSpec, JobState, JobTransitionError
from .projects import ProjectRecord
from .providers import (
    ExecutionMode,
    MediaType,
    ModelRequirement,
    ProviderCapability,
    QualityTier,
    ResumeStrategy,
)
from .reviews import ReviewRecord, ReviewStatus

__all__ = [
    "ArtifactStatus",
    "ArtifactType",
    "ArtifactVersion",
    "EnqueueResult",
    "ExecutionMode",
    "GenerationJob",
    "JobSpec",
    "JobState",
    "JobTransitionError",
    "MediaType",
    "ModelRequirement",
    "ProjectRecord",
    "ProviderCapability",
    "QualityTier",
    "ResumeStrategy",
    "ReviewRecord",
    "ReviewStatus",
    "ShotReadiness",
    "ShotState",
    "affected_artifact_types",
    "compute_input_hash",
]
