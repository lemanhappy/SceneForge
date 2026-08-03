import tempfile
import unittest

from agent_runtime.session_index import SessionIndex
from repositories.session_repository import SessionRepository


class SessionRepositoryContractTests(unittest.TestCase):
    def test_session_index_satisfies_repository_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = SessionIndex(tmp)
            self.assertIsInstance(repository, SessionRepository)

            first = repository.create(idea="first", session_id="first")
            second = repository.create(idea="second", session_id="second")
            repository.update_stage(first["session_id"], "script_review_pending", "ready")
            repository.create_review_task(first["session_id"], "script", "review")

            records = repository.list_sessions()
            self.assertEqual({item["session_id"] for item in records}, {"first", "second"})
            self.assertEqual(repository.get("first")["stage"], "script_review_pending")
            self.assertEqual(repository.list_review_tasks("first")[0]["status"], "pending")
            self.assertEqual(repository.active()["session_id"], second["session_id"])

    def test_list_sessions_returns_defaults_without_exposing_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = SessionIndex(tmp)
            repository.create(session_id="legacy")
            data = repository.load()
            data["sessions"]["legacy"].pop("review_tasks", None)
            repository.save(data)

            records = repository.list_sessions()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["session_id"], "legacy")
            self.assertEqual(records[0]["review_tasks"], [])
