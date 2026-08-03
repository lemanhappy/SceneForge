from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SessionRepository(Protocol):
    """Compatibility facade implemented by the current ``SessionIndex``.

    The public name stays session-oriented until the frontend route migration;
    internally each session is treated as a project.
    """

    def active(self) -> dict[str, Any] | None: ...

    def get(self, session_id: str) -> dict[str, Any] | None: ...

    def list_sessions(self) -> list[dict[str, Any]]: ...

    def create(
        self,
        idea: str = "",
        user_requirement: str = "",
        style: str = "",
        session_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]: ...

    def set_active(self, session_id: str) -> dict[str, Any]: ...

    def update_stage(self, session_id: str, stage: str, summary: str = "") -> None: ...

    def update_metadata(
        self,
        session_id: str,
        *,
        idea: str = "",
        user_requirement: str = "",
        style: str = "",
    ) -> dict[str, Any]: ...

    def mark_stale(self, session_id: str, keys: list[str]) -> None: ...

    def create_review_task(
        self,
        session_id: str,
        stage: str,
        summary: str = "",
        artifact_refs: list[str] | None = None,
        artifact_version: str = "v1",
    ) -> dict[str, Any]: ...

    def list_review_tasks(self, session_id: str) -> list[dict[str, Any]]: ...

    def resolve_review_task(self, session_id: str, review_id: str, status: str) -> dict[str, Any]: ...

    def delete(self, session_id: str) -> bool: ...
