import tempfile
import unittest
from pathlib import Path

from agent_runtime.session_index import SessionIndex


class SessionIndexTests(unittest.TestCase):
    def test_create_session_and_checklist(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            record = index.create(
                idea="Moon cat",
                user_requirement="short",
                style="anime",
                character_asset_ids=["lead"],
                prop_asset_ids=["moon_lamp"],
                scene_asset_ids=["roof"],
                continuity_source_session_id="episode-1",
            )
            self.assertEqual(index.active()["session_id"], record["session_id"])
            working_dir = Path(tmp) / record["working_dir"]
            self.assertTrue((working_dir / "idea2video").exists())
            self.assertTrue((working_dir / "script2video").exists())
            checklist = index.artifact_checklist(record["session_id"])
            self.assertFalse(checklist["script2video/storyboard.json"])
            self.assertFalse(checklist["idea2video/scene_*/storyboard.json"])
            self.assertEqual(record["compacted_summary"], "")
            self.assertEqual(record["compaction_snapshots"], [])
            self.assertEqual(record["character_asset_ids"], ["lead"])
            self.assertEqual(record["prop_asset_ids"], ["moon_lamp"])
            self.assertEqual(record["scene_asset_ids"], ["roof"])
            self.assertEqual(record["continuity_source_session_id"], "episode-1")


    def test_delete_removes_record_working_dir_and_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            rec = index.create(idea="to delete")
            sid = rec["session_id"]
            wd = Path(tmp) / rec["working_dir"]
            (wd / "idea2video" / "marker.txt").write_text("x", encoding="utf-8")
            self.assertTrue(wd.exists())
            self.assertEqual(index.active()["session_id"], sid)

            self.assertTrue(index.delete(sid))
            self.assertIsNone(index.get(sid))          # gone from index
            self.assertFalse(wd.exists())              # working dir removed
            self.assertIsNone(index.active())          # active cleared
            self.assertFalse(index.delete(sid))        # second delete -> no-op

    def test_delete_unknown_is_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(SessionIndex(tmp).delete("nope"))

    def test_session_id_is_sanitized_and_stays_under_working_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            record = index.create(session_id="../../escaped-review")
            self.assertEqual(record["session_id"], "escaped-review")
            working_dir = (Path(tmp) / record["working_dir"]).resolve()
            self.assertTrue(str(working_dir).startswith(str((Path(tmp) / ".working_dir").resolve())))
            self.assertFalse((Path(tmp).parent / "escaped-review").exists())


    def test_update_compaction_writes_session_state_not_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            record = index.create(idea="compact")
            index.update_compaction(record["session_id"], {
                "summary": "## Reference Context Only\n- old context",
                "compacted_message_count": 4,
                "preserved_message_count": 2,
                "estimated_tokens_before": 1000,
                "estimated_tokens_after": 300,
                "reason": "manual",
                "mode": "fallback-local",
            })
            session = index.get(record["session_id"])
            self.assertIn("old context", session["compacted_summary"])
            self.assertEqual(session["compacted_turns"], 2)
            self.assertEqual(session["last_compaction_reason"], "manual")
            self.assertTrue(session["compaction_snapshots"])
            self.assertNotIn("old context", index.memory_text())

    def test_memory_and_turn_record_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            record = index.create()
            index.write_memory("# User Preferences\n- 16:9\n")
            self.assertIn("16:9", index.memory_text())
            index.append_turn_record(record["session_id"], {"turn_id": "t1", "status": "completed", "tool_rounds": [], "final_assistant_text": "done"})
            self.assertTrue((Path(tmp) / ".sceneforge" / "logs" / "loop_history.jsonl").exists())
            self.assertEqual(len(index.get(record["session_id"])["recent_turn_records"]), 1)
