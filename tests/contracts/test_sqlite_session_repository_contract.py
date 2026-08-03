import tempfile
import threading
import unittest
from pathlib import Path

from agent_runtime.session_index import SessionIndex
from infrastructure.sqlite import SQLiteDatabase, SQLiteSessionStateStore
from repositories.session_repository import SessionRepository


class SQLiteSessionRepositoryContractTests(unittest.TestCase):
    def _index(self, root: str | Path) -> SessionIndex:
        root = Path(root)
        database = SQLiteDatabase(root / ".sceneforge" / "sceneforge.db")
        return SessionIndex(root, state_store=SQLiteSessionStateStore(database))

    def test_full_session_and_review_contract_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = self._index(tmp)
            self.assertIsInstance(index, SessionRepository)
            record = index.create(idea="离别", style="电影感", session_id="drama")
            index.update_stage(record["session_id"], "script_review_pending", "ready")
            review = index.create_review_task(record["session_id"], "script", "请审核")
            index.resolve_review_task(record["session_id"], review["review_id"], "approved")

            reopened = self._index(tmp)
            stored = reopened.get("drama")
            self.assertEqual(stored["idea"], "离别")
            self.assertEqual(stored["stage"], "script_review_pending")
            self.assertEqual(stored["summary"], "ready")
            self.assertEqual(reopened.active()["session_id"], "drama")
            self.assertEqual(reopened.list_review_tasks("drama")[0]["status"], "approved")

    def test_unknown_legacy_fields_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = self._index(tmp)
            record = index.create(session_id="legacy")
            state = index.load()
            state["sessions"][record["session_id"]]["future_field"] = {"nested": True}
            index.save(state)
            self.assertEqual(self._index(tmp).get("legacy")["future_field"], {"nested": True})

    def test_concurrent_instances_do_not_lose_projects(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = self._index(tmp)
            second = self._index(tmp)

            def create_many(index, prefix):
                for number in range(30):
                    index.create(session_id=f"{prefix}-{number}")

            workers = [
                threading.Thread(target=create_many, args=(first, "a")),
                threading.Thread(target=create_many, args=(second, "b")),
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()

            self.assertEqual(len(self._index(tmp).list_sessions()), 60)

    def test_failed_save_rolls_back_all_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = self._index(tmp)
            index.create(session_id="safe")
            invalid = index.load()
            invalid["active_session_id"] = "missing"
            with self.assertRaises(ValueError):
                index.save(invalid)
            self.assertEqual(self._index(tmp).active()["session_id"], "safe")
