from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable

from domain.artifacts import ArtifactType, ArtifactVersion, ShotReadiness, ShotState


@runtime_checkable
class ArtifactRepository(Protocol):
    def get_shot_state(
        self, project_id: str, scene_index: int, shot_index: int
    ) -> ShotState | None: ...

    def set_shot_state(
        self,
        project_id: str,
        scene_index: int,
        shot_index: int,
        readiness: ShotReadiness | str,
        *,
        input_hash: str = "",
        stale_reason: str | None = None,
    ) -> ShotState: ...

    def create_version(
        self,
        project_id: str,
        scene_index: int,
        shot_index: int,
        artifact_type: ArtifactType | str,
        *,
        input_hash: str,
        relative_path: str,
        inputs: Mapping[str, str] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ArtifactVersion: ...

    def get_version(self, artifact_id: str) -> ArtifactVersion | None: ...

    def list_versions(
        self,
        project_id: str,
        scene_index: int,
        shot_index: int,
        artifact_type: ArtifactType | str,
    ) -> list[ArtifactVersion]: ...

    def mark_inputs_changed(
        self,
        project_id: str,
        scene_index: int,
        shot_index: int,
        changed_type: ArtifactType | str,
        *,
        input_hash: str,
        reason: str,
    ) -> list[ArtifactVersion]: ...

    def activate_version(self, artifact_id: str) -> ArtifactVersion: ...
