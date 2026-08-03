"""Tests for production admin routes: batch cleanup-all + job-list history."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from server.production_api import ProductionAPI
from services.housekeeping import HousekeepingService
from services.job_runner import JobRunner


class _FakeIndex:
    def __init__(self, sessions):
        self._sessions = sessions  # {sid: working_dir}

    def load(self):
        return {"sessions": {sid: {} for sid in self._sessions}}

    def list_sessions(self):
        return [{"session_id": sid} for sid in self._sessions]

    def working_dir(self, sid):
        return self._sessions[sid]

    def get(self, sid):
        return {} if sid in self._sessions else None


def _session_dir(base, sid, with_final=True):
    wd = Path(base) / sid
    shot = wd / "shots" / "0"
    shot.mkdir(parents=True)
    (shot / "video.mp4").write_bytes(b"v" * 500)
    (shot / "first_frame.png").write_bytes(b"x" * 100)
    if with_final:
        (wd / "final_video.mp4").write_bytes(b"f" * 1000)
    return str(wd)


class _FakeService:
    def __init__(self, runner):
        self.runner = runner


class TestCleanupAll(unittest.TestCase):
    def test_preview_and_cleanup(self):
        with tempfile.TemporaryDirectory() as d:
            s1 = _session_dir(d, "s1", with_final=True)
            s2 = _session_dir(d, "s2", with_final=False)  # no final -> skipped
            index = _FakeIndex({"s1": s1, "s2": s2})
            api = ProductionAPI(index, _FakeService(JobRunner()), adapters=None,
                                housekeeping=HousekeepingService())
            status, prev = asyncio.run(api.handle("GET", "/api/production/cleanup-all", {}))
            self.assertEqual(status, 200)
            self.assertEqual(prev["sessions_with_final"], 1)
            self.assertGreater(prev["freeable_mb"], -1)
            status, res = asyncio.run(api.handle("POST", "/api/production/cleanup-all", {}))
            self.assertEqual(status, 200)
            self.assertEqual(res["cleaned_sessions"], 1)  # only s1 had a final
            self.assertFalse((Path(s1) / "shots" / "0" / "video.mp4").exists())
            self.assertTrue((Path(s1) / "final_video.mp4").exists())
            self.assertTrue((Path(s2) / "shots" / "0" / "video.mp4").exists())  # untouched


class _SnapIndex(_FakeIndex):
    def list_review_tasks(self, sid):
        return []
    def artifact_checklist(self, sid):
        return {}


class _Runner:
    def is_running(self, sid):
        return False

    def running_job_id(self, sid):
        return None


class _SnapService:
    def __init__(self):
        self.runner = _Runner()


class TestHasFinal(unittest.TestCase):
    def _api(self, adapters):
        return ProductionAPI(_SnapIndex({"s1": "/wd"}), _SnapService(), adapters=adapters)

    def test_no_adapters_means_no_final(self):
        api = self._api(None)
        self.assertFalse(api._has_final("s1"))
        # and the snapshot exposes the flag for the UI to gate 看成片/发布/清理
        self.assertIn("has_final", api._snapshot("s1"))
        self.assertFalse(api._snapshot("s1")["has_final"])

    def test_adapter_reporting_final_is_true(self):
        class _Ad:
            def _find_final_video(self, wd):
                return Path(wd) / "final_video.mp4"  # truthy -> has final
        self.assertTrue(self._api(_Ad())._has_final("s1"))

    def test_adapter_error_fails_safe(self):
        class _Boom:
            def _find_final_video(self, wd):
                raise RuntimeError("boom")
        self.assertFalse(self._api(_Boom())._has_final("s1"))  # guarded, not raised


class TestJobList(unittest.TestCase):
    def test_jobs_list_route(self):
        runner = JobRunner()
        api = ProductionAPI(_FakeIndex({}), _FakeService(runner), adapters=None)
        status, body = asyncio.run(api.handle("GET", "/api/production/jobs", {}))
        self.assertEqual(status, 200)
        self.assertEqual(body["jobs"], [])

    def test_list_recent_summarizes_progress(self):
        runner = JobRunner()
        prog = [{"stage": "x", "message": "step1"}, {"stage": "x", "message": "step2"}]
        import time
        rec = runner.submit(lambda: _done(), progress=prog)
        time.sleep(0.1)
        recent = runner.list_recent()
        self.assertEqual(recent[0]["steps"], 2)
        self.assertEqual(recent[0]["last"], "step2")


async def _done():
    return "ok"


if __name__ == "__main__":
    unittest.main()
