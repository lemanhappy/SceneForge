from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SessionStateStore(Protocol):
    """Atomic storage for the legacy SessionIndex envelope during migration."""

    def locked(self) -> AbstractContextManager[None]: ...

    def load(self) -> dict[str, Any]: ...

    def save(self, data: dict[str, Any]) -> None: ...
