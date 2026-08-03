import json
import tempfile
import unittest
from pathlib import Path

from agent_runtime.session_index import SessionIndex
from infrastructure.legacy import LegacySessionImporter
from infrastructure.sqlite import SQLiteDatabase


class LegacySessionImporterTests(unittest.TestCase):
    def test_scan_import_and_idempotent_reimport(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy_root = root / "legacy"
            index = SessionIndex(legacy_root)
            record = index.create(idea="中文短剧", session_id="drama-one")
            index.create_review_task(record["session_id"], "script", "ready")

            database = SQLiteDatabase(root / "data" / "sceneforge.db")
            importer = LegacySessionImporter(database)
            preview = importer.scan(index.sessions_path)
            self.assertEqual((preview.project_count, preview.review_count), (1, 1))
            self.assertFalse(preview.errors)

            imported = importer.import_file(index.sessions_path)
            self.assertTrue(imported.imported)
            repeated = importer.import_file(index.sessions_path)
            self.assertTrue(repeated.already_imported)

            with database.connection() as connection:
                project = connection.execute(
                    "SELECT project_id, record_json FROM projects WHERE project_id = 'drama-one'"
                ).fetchone()
                review_count = connection.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
                active = json.loads(
                    connection.execute("SELECT value_json FROM app_state WHERE key = 'active_session_id'").fetchone()[0]
                )
            self.assertEqual(project["project_id"], "drama-one")
            self.assertEqual(json.loads(project["record_json"])["idea"], "中文短剧")
            self.assertEqual(review_count, 1)
            self.assertEqual(active, "drama-one")
            self.assertTrue(index.sessions_path.exists(), "explicit import must not delete the source file")

    def test_invalid_legacy_record_is_not_imported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sessions.json"
            source.write_text(
                json.dumps({"active_session_id": "bad", "sessions": {"bad": {"idea": "missing path"}}}),
                encoding="utf-8",
            )
            database = SQLiteDatabase(root / "sceneforge.db")
            importer = LegacySessionImporter(database)
            self.assertTrue(importer.scan(source).errors)
            with self.assertRaises(ValueError):
                importer.import_file(source)
            self.assertFalse(database.path.exists(), "validation failure must not create a target database")

    def test_modified_legacy_source_does_not_overwrite_existing_project_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy_root = root / "legacy"
            index = SessionIndex(legacy_root)
            index.create(idea="legacy", session_id="same-project")
            database = SQLiteDatabase(root / "sceneforge.db")
            importer = LegacySessionImporter(database)
            importer.import_file(index.sessions_path)

            payload = index.load()
            payload["sessions"]["same-project"]["idea"] = "changed legacy"
            index.save(payload)
            with self.assertRaisesRegex(ValueError, "conflicts with existing projects"):
                importer.import_file(index.sessions_path)

            with database.connection() as connection:
                stored = connection.execute(
                    "SELECT record_json FROM projects WHERE project_id = ?",
                    ("same-project",),
                ).fetchone()
            self.assertEqual(json.loads(stored["record_json"])["idea"], "legacy")
