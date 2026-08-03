from __future__ import annotations

import os
from pathlib import Path

from infrastructure.legacy import LegacySessionImporter
from infrastructure.sqlite import SQLiteDatabase, SQLiteSessionStateStore
from project_identity import state_directory

from .session_index import SessionIndex


class SessionBootstrapError(RuntimeError):
    pass


def create_session_index(
    workspace_root: str | Path = ".",
    *,
    backend: str | None = None,
    database_path: str | Path | None = None,
    auto_import_legacy: bool = True,
) -> SessionIndex:
    """Build the shared session facade for every executable entry point."""

    root = Path(workspace_root).resolve()
    state_root = state_directory(root)
    selected = str(backend or os.environ.get("SCENEFORGE_SESSION_BACKEND", "sqlite")).strip().lower()
    if selected == "json":
        return SessionIndex(root)
    if selected != "sqlite":
        raise ValueError(f"unsupported session backend: {selected}")

    db_path = Path(database_path).resolve() if database_path else state_root / "sceneforge.db"
    database = SQLiteDatabase(db_path)
    store = SQLiteSessionStateStore(database)
    legacy_path = state_root / "sessions.json"
    if auto_import_legacy and not store.has_projects() and legacy_path.exists():
        importer = LegacySessionImporter(database)
        report = importer.scan(legacy_path)
        if report.errors:
            raise SessionBootstrapError(
                "cannot import legacy sessions.json: " + "; ".join(report.errors)
            )
        if report.project_count:
            importer.import_file(legacy_path)
    return SessionIndex(root, state_store=store)
