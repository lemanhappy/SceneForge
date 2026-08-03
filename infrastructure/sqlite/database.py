from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
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
