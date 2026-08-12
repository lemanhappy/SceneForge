from __future__ import annotations

import argparse
from contextlib import closing
import os
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from infrastructure.sqlite import SQLiteDatabase
from project_identity import state_directory
from services.database_maintenance import create_database_backup, prune_database_backups


def _database_path(workspace: str) -> Path:
    return state_directory(Path(workspace).resolve()) / "sceneforge.db"


def _backup_dir(workspace: str) -> Path:
    return state_directory(Path(workspace).resolve()) / "backups"


def _restore(source: Path, destination: Path, *, force: bool) -> None:
    if not force:
        raise SystemExit("restore requires --force; stop SceneForge before restoring")
    if not source.is_file():
        raise SystemExit(f"backup not found: {source}")
    if source == destination:
        raise SystemExit("backup source must differ from the active database")
    ok, messages = SQLiteDatabase(source).integrity_check(quick=False)
    if not ok:
        raise SystemExit("backup integrity check failed: " + "; ".join(messages))
    if destination.is_file() and destination.stat().st_size:
        emergency = create_database_backup(destination, destination.parent / "backups")
        print(f"Current database backed up to {emergency}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".restore.tmp")
    temporary.unlink(missing_ok=True)
    try:
        with closing(sqlite3.connect(source)) as src:
            with closing(sqlite3.connect(temporary)) as target:
                src.backup(target)
        for suffix in ("-wal", "-shm"):
            destination.with_name(destination.name + suffix).unlink(missing_ok=True)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Check, back up, or restore the SceneForge database")
    parser.add_argument("action", choices=("check", "backup", "list", "restore"))
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--source", help="Backup file to restore")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--keep", type=int, default=7)
    args = parser.parse_args()
    database_path = _database_path(args.workspace)
    backup_dir = _backup_dir(args.workspace)

    if args.action == "check":
        if not database_path.is_file():
            raise SystemExit(f"database not found: {database_path}")
        ok, messages = SQLiteDatabase(database_path).integrity_check(quick=False)
        print("OK" if ok else "FAILED: " + "; ".join(messages))
        raise SystemExit(0 if ok else 1)
    if args.action == "backup":
        backup = create_database_backup(database_path, backup_dir)
        prune_database_backups(backup_dir, keep=args.keep)
        print(backup)
        return
    if args.action == "list":
        for path in sorted(backup_dir.glob("sceneforge-*.db"), reverse=True):
            print(path)
        return
    if not args.source:
        raise SystemExit("restore requires --source BACKUP.db")
    _restore(Path(args.source).resolve(), database_path, force=args.force)
    print(f"Restored {database_path}")


if __name__ == "__main__":
    main()
