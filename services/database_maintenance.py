from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from infrastructure.sqlite import SQLiteDatabase


class DatabaseIntegrityError(RuntimeError):
    pass


def create_database_backup(database_path: str | Path, backup_dir: str | Path) -> Path:
    source = SQLiteDatabase(database_path)
    ok, messages = source.integrity_check(quick=False)
    if not ok:
        raise DatabaseIntegrityError("database integrity check failed: " + "; ".join(messages))
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = Path(backup_dir).resolve() / f"sceneforge-{timestamp}.db"
    counter = 1
    while target.exists():
        target = target.with_name(f"sceneforge-{timestamp}-{counter}.db")
        counter += 1
    return source.backup_to(target)


def prune_database_backups(backup_dir: str | Path, *, keep: int = 7) -> list[Path]:
    root = Path(backup_dir).resolve()
    if not root.is_dir():
        return []
    backups = sorted(root.glob("sceneforge-*.db"), key=lambda item: item.stat().st_mtime, reverse=True)
    removed = []
    for path in backups[max(1, int(keep)):]:
        path.unlink(missing_ok=True)
        removed.append(path)
    return removed


def prepare_database_startup(
    database_path: str | Path,
    backup_dir: str | Path,
    *,
    keep: int = 7,
) -> Path | None:
    path = Path(database_path).resolve()
    if not path.is_file() or path.stat().st_size == 0:
        return None
    backup = create_database_backup(path, backup_dir)
    prune_database_backups(backup_dir, keep=keep)
    return backup
