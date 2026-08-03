"""Tests for live job progress: the engine's progress sink lands on the job
record and is exposed by JobRunner.get for polling clients."""

import time
import unittest

from services.job_runner import JobRunner
from services.production_service import ProductionService


def _await_done(runner, job_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = runner.get(job_id)
        if j and j["state"] != "running":
            return j
        time.sleep(0.02)
    return runner.get(job_id)


class _FakeEngine:
    async def approve(self, session_id, progress=None):
        if progress:
            progress("video_clip_start", "rendering shot 1")
            progress("video_clip_start", "rendering shot 2")
        return {"ok": True, "stage": "shot_video", "summary": "done"}

    async def start_topic(self, idea, user_requirement="", style="", domain="", character_asset_ids=None,
                          prop_asset_ids=None, scene_asset_ids=None, mode="idea", script="",
                          target_language=None, aspect_ratio=None, overrides=None,
                          quality_tier="balanced", progress=None):
        if progress:
            progress("script", "writing script")
        return {"ok": True, "stage": "script", "summary": "scripted"}

    async def revise(self, session_id, instruction, progress=None):
        return {"ok": True, "stage": "script"}


class TestJobProgress(unittest.TestCase):
    def test_submit_exposes_progress_list(self):
        runner = JobRunner()
        prog = []

        async def work():
            prog.append({"stage": "x", "message": "step"})
            return "ok"

        rec = runner.submit(work, progress=prog)
        j = _await_done(runner, rec["job_id"])
        self.assertEqual(j["state"], "done")
        self.assertEqual(j["progress"], [{"stage": "x", "message": "step"}])

    def test_approve_threads_progress_to_job(self):
        runner = JobRunner()
        svc = ProductionService(engine=_FakeEngine(), runner=runner)
        rec = svc.approve("s1")
        j = _await_done(runner, rec["job_id"])
        self.assertEqual(j["state"], "done")
        msgs = [p["message"] for p in j["progress"]]
        self.assertEqual(msgs, ["rendering shot 1", "rendering shot 2"])

    def test_start_topic_threads_progress(self):
        runner = JobRunner()
        svc = ProductionService(engine=_FakeEngine(), runner=runner)
        rec = svc.start_topic("一个程序员的逆袭")
        j = _await_done(runner, rec["job_id"])
        self.assertEqual([p["message"] for p in j["progress"]], ["writing script"])

    def test_sink_forwards_whitelisted_meta_only(self):
        # the sink keeps numeric/id meta (for the UI progress bar) but drops
        # paths and other bulky/sensitive fields.
        prog = []
        report = ProductionService._sink(prog)
        report("video_clip_done", "shot 3 done",
               {"shot_idx": 3, "shot_count": 8, "path": "C:/secret/working/shots/3/video.mp4"})
        self.assertEqual(prog[0]["meta"], {"shot_idx": 3, "shot_count": 8})
        # an entry with no whitelisted meta carries no meta key at all
        report("render_done", "complete", {"final_video_path": "C:/x.mp4"})
        self.assertNotIn("meta", prog[1])

    def test_progress_snapshot_is_copied(self):
        # get() must return a copy so callers don't see later mutations of the
        # same list object.
        runner = JobRunner()
        prog = []

        async def work():
            time.sleep(0.05)
            prog.append({"stage": "late", "message": "late"})
            return "ok"

        rec = runner.submit(work, progress=prog)
        early = runner.get(rec["job_id"])
        _await_done(runner, rec["job_id"])
        # the early snapshot is frozen, not retroactively mutated
        self.assertEqual(early["progress"], [])


if __name__ == "__main__":
    unittest.main()
