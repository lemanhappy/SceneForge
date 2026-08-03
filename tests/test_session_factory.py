import tempfile
import unittest
from pathlib import Path

from agent_runtime.session_factory import SessionBootstrapError, create_session_index
from agent_runtime.session_index import SessionIndex


class SessionFactoryTests(unittest.TestCase):
    def test_sqlite_bootstrap_imports_existing_json_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = SessionIndex(root)
            record = legacy.create(idea="旧项目", session_id="legacy-project")
            legacy.create_review_task(record["session_id"], "storyboard", "review")
            source_before = legacy.sessions_path.read_bytes()

            sqlite_index = create_session_index(root, backend="sqlite")
            self.assertEqual(sqlite_index.get("legacy-project")["idea"], "旧项目")
            self.assertEqual(len(sqlite_index.list_review_tasks("legacy-project")), 1)
            self.assertEqual(legacy.sessions_path.read_bytes(), source_before)

            reopened = create_session_index(root, backend="sqlite")
            self.assertEqual(len(reopened.list_sessions()), 1)
            self.assertEqual(len(reopened.list_review_tasks("legacy-project")), 1)

    def test_json_backend_remains_an_explicit_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index = create_session_index(root, backend="json")
            index.create(session_id="json-only")
            self.assertTrue(root.joinpath(".sceneforge", "sessions.json").exists())
            self.assertFalse(root.joinpath(".sceneforge", "sceneforge.db").exists())

    def test_invalid_legacy_json_stops_bootstrap_without_overwriting_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / ".sceneforge" / "sessions.json"
            source.parent.mkdir(parents=True)
            source.write_text("{broken", encoding="utf-8")
            with self.assertRaises(SessionBootstrapError):
                create_session_index(root, backend="sqlite")
            self.assertEqual(source.read_text(encoding="utf-8"), "{broken")

    def test_sqlite_is_the_default_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index = create_session_index(root)
            index.create(session_id="sqlite-default")
            self.assertTrue(root.joinpath(".sceneforge", "sceneforge.db").exists())
            self.assertFalse(root.joinpath(".sceneforge", "sessions.json").exists())
