from __future__ import annotations

import sqlite3
import threading
import os
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Iterator


class SQLiteDatabase:
    """Connection factory with the durability settings required by the desktop app."""

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 30_000) -> None:
        self.path = Path(path).resolve()
        self.busy_timeout_ms = max(1, int(busy_timeout_ms))
        self._configured = False
        self._configure_lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def open(self) -> sqlite3.Connection:
        self.configure()
        return self._open_raw()

    def configure(self) -> None:
        if self._configured:
            return
        with self._configure_lock:
            if self._configured:
                return
            connection = self._open_raw()
            try:
                connection.execute("PRAGMA journal_mode = WAL")
            finally:
                connection.close()
            self._configured = True

    def _open_raw(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self.open()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def migrate(self) -> int:
        from .migrator import SQLiteMigrator

        return SQLiteMigrator(self).migrate()

    def integrity_check(self, *, quick: bool = True) -> tuple[bool, list[str]]:
        pragma = "quick_check" if quick else "integrity_check"
        with self.connection() as connection:
            messages = [str(row[0]) for row in connection.execute(f"PRAGMA {pragma}").fetchall()]
        return messages == ["ok"], messages

    def backup_to(self, destination: str | Path) -> Path:
        """Create and verify an atomic SQLite backup, including WAL content."""
        target = Path(destination).resolve()
        if target == self.path:
            raise ValueError("backup destination must differ from database path")
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(
            prefix=f".{target.stem}-",
            suffix=".tmp",
            dir=target.parent,
        )
        os.close(handle)
        temp_path = Path(temp_name)
        try:
            with self.connection() as source:
                with closing(sqlite3.connect(temp_path)) as backup:
                    source.backup(backup)
                    messages = [str(row[0]) for row in backup.execute("PRAGMA integrity_check").fetchall()]
                    if messages != ["ok"]:
                        raise sqlite3.DatabaseError("backup integrity check failed: " + "; ".join(messages))
            os.replace(temp_path, target)
            return target
        finally:
            temp_path.unlink(missing_ok=True)
