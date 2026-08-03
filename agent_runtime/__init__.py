from __future__ import annotations

from project_identity import apply_legacy_environment

apply_legacy_environment()

__all__ = ["AgentLoop", "SessionIndex", "ToolRegistry", "build_runtime", "create_session_index"]


def build_runtime(*args, **kwargs):
    from .loop import build_runtime as _build_runtime

    return _build_runtime(*args, **kwargs)


def __getattr__(name):
    if name == "AgentLoop":
        from .loop import AgentLoop

        return AgentLoop
    if name == "SessionIndex":
        from .session_index import SessionIndex

        return SessionIndex
    if name == "create_session_index":
        from .session_factory import create_session_index

        return create_session_index
    if name == "ToolRegistry":
        from .tools import ToolRegistry

        return ToolRegistry
    raise AttributeError(name)
