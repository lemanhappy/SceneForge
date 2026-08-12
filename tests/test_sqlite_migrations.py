import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

from infrastructure.sqlite import MigrationError, SQLiteDatabase, SQLiteMigrator
from services.database_maintenance import (
    create_database_backup,
    prepare_database_startup,
    prune_database_backups,
)


class SQLiteMigrationTests(unittest.TestCase):
    def test_fresh_database_and_repeat_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = SQLiteDatabase(Path(tmp) / "data" / "sceneforge.db")
            self.assertEqual(database.migrate(), 7)
            self.assertEqual(database.migrate(), 7)
            with database.connection() as connection:
                tables = {
                    row["name"]
                    for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
                }
                journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
                foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
                job_columns = {
                    row["name"] for row in connection.execute("PRAGMA table_info(generation_jobs)")
                }
                project_columns = {
                    row["name"] for row in connection.execute("PRAGMA table_info(projects)").fetchall()
                }
            self.assertTrue(
                {
                    "projects",
                    "reviews",
                    "generation_jobs",
                    "provider_profiles",
                    "shots",
                    "artifacts",
                    "artifact_inputs",
                    "assets",
                    "character_identities",
                    "reference_sets",
                    "outfit_versions",
                    "render_bindings",
                }
                <= tables
            )
            self.assertEqual(journal_mode.lower(), "wal")
            self.assertEqual(foreign_keys, 1)
            self.assertTrue(
                {"remote_provider", "remote_metadata_json", "remote_artifact_path"} <= job_columns
            )
            self.assertIn("series", tables)
            self.assertTrue(
                {"series_id", "episode_number", "episode_title", "previous_episode_id"} <= project_columns
            )

    def test_changed_applied_migration_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            migrations = root / "migrations"
            migrations.mkdir()
            migration = migrations / "001_test.sql"
            migration.write_text("CREATE TABLE sample(id INTEGER PRIMARY KEY);", encoding="utf-8")
            database = SQLiteDatabase(root / "sceneforge.db")
            SQLiteMigrator(database, migrations).migrate()
            migration.write_text("CREATE TABLE sample(id TEXT PRIMARY KEY);", encoding="utf-8")
            with self.assertRaises(MigrationError):
                SQLiteMigrator(database, migrations).migrate()

    def test_failed_migration_rolls_back_schema_and_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            migrations = root / "migrations"
            migrations.mkdir()
            (migrations / "001_ok.sql").write_text("CREATE TABLE stable(id INTEGER);", encoding="utf-8")
            (migrations / "002_bad.sql").write_text(
                "CREATE TABLE partial(id INTEGER); THIS IS NOT SQL;", encoding="utf-8"
            )
            database = SQLiteDatabase(root / "sceneforge.db")
            with self.assertRaises(MigrationError):
                SQLiteMigrator(database, migrations).migrate()
            with database.connection() as connection:
                names = {
                    row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
                }
                version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            self.assertIn("stable", names)
            self.assertNotIn("partial", names)
            self.assertEqual(version, 1)

    def test_verified_backup_contains_committed_wal_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = SQLiteDatabase(root / "sceneforge.db")
            with database.transaction() as connection:
                connection.execute("CREATE TABLE sample(value TEXT)")
                connection.execute("INSERT INTO sample VALUES ('saved')")

            backup = create_database_backup(database.path, root / "backups")

            self.assertTrue(backup.is_file())
            ok, messages = SQLiteDatabase(backup).integrity_check(quick=False)
            self.assertTrue(ok, messages)
            with closing(sqlite3.connect(backup)) as connection:
                self.assertEqual(connection.execute("SELECT value FROM sample").fetchone()[0], "saved")

    def test_startup_backup_skips_new_database_and_prunes_oldest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "sceneforge.db"
            backups = root / "backups"
            self.assertIsNone(prepare_database_startup(path, backups))
            database = SQLiteDatabase(path)
            with database.transaction() as connection:
                connection.execute("CREATE TABLE sample(value TEXT)")
            first = create_database_backup(path, backups)
            time.sleep(0.01)
            second = create_database_backup(path, backups)
            time.sleep(0.01)
            third = create_database_backup(path, backups)

            removed = prune_database_backups(backups, keep=2)

            self.assertEqual(removed, [first])
            self.assertTrue(second.exists())
            self.assertTrue(third.exists())
