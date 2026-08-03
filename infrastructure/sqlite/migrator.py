from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .database import SQLiteDatabase


_MIGRATION_NAME = re.compile(r"^(?P<version>\d{3,})_(?P<name>[a-z0-9_]+)\.sql$")


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    path: Path
    sql: str
    checksum: str


class SQLiteMigrator:
    def __init__(self, database: SQLiteDatabase, migrations_dir: str | Path | None = None) -> None:
        self.database = database
        self.migrations_dir = Path(migrations_dir or Path(__file__).with_name("migrations")).resolve()

    def discover(self) -> list[Migration]:
        migrations: list[Migration] = []
        seen: set[int] = set()
        for path in sorted(self.migrations_dir.glob("*.sql")):
            match = _MIGRATION_NAME.match(path.name)
            if match is None:
                continue
            version = int(match.group("version"))
            if version in seen:
                raise MigrationError(f"duplicate migration version: {version}")
            seen.add(version)
            sql = path.read_text(encoding="utf-8")
            migrations.append(
                Migration(
                    version=version,
                    name=match.group("name"),
                    path=path,
                    sql=sql,
                    checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
                )
            )
        return sorted(migrations, key=lambda migration: migration.version)

    def migrate(self) -> int:
        migrations = self.discover()
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
                """
            )
            applied_versions = {
                int(row["version"]): row
                for row in connection.execute(
                    "SELECT version, name, checksum, applied_at FROM schema_migrations"
                ).fetchall()
            }

        known_versions = {migration.version for migration in migrations}
        unknown = sorted(set(applied_versions) - known_versions)
        if unknown:
            raise MigrationError(f"database contains unknown migration versions: {unknown}")

        for migration in migrations:
            with self.database.transaction(immediate=True) as connection:
                previous = connection.execute(
                    "SELECT version, name, checksum FROM schema_migrations WHERE version = ?",
                    (migration.version,),
                ).fetchone()
                if previous is not None:
                    if previous["checksum"] != migration.checksum:
                        raise MigrationError(
                            f"migration {migration.version:03d}_{migration.name} changed after it was applied"
                        )
                    continue
                self._apply(connection, migration)
        return migrations[-1].version if migrations else 0

    def current_version(self) -> int:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT MAX(version) AS version FROM schema_migrations"
            ).fetchone()
        return int(row["version"] or 0) if row is not None else 0

    @staticmethod
    def _apply(connection: sqlite3.Connection, migration: Migration) -> None:
        applied_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            for statement in _sql_statements(migration.sql):
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
                (migration.version, migration.name, migration.checksum, applied_at),
            )
        except BaseException as exc:
            raise MigrationError(
                f"failed to apply migration {migration.version:03d}_{migration.name}: {exc}"
            ) from exc


def _sql_statements(script: str) -> list[str]:
    statements: list[str] = []
    buffer = ""
    for character in script:
        buffer += character
        if character == ";" and sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                statements.append(statement)
            buffer = ""
    if buffer.strip():
        if not sqlite3.complete_statement(buffer):
            raise MigrationError("migration contains an incomplete SQL statement")
        statements.append(buffer.strip())
    return statements
