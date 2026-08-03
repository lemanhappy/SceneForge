import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent_runtime.session_factory import create_session_index
from domain.jobs import JobSpec, JobState
from infrastructure.sqlite import SQLiteDatabase, SQLiteJobQueue
from interfaces.video_output import VideoOutput
from services import DurableJobRunner
from services.job_handlers import JobContext
from services.remote_recovery import (
    RemoteRecoveryAction,
    RemoteVideoRecovery,
)
from tools.remote_video import RemoteVideoInspection, RemoteVideoState


class _CompletedProvider:
    def __init__(self, data=b"recovered-video"):
        self.data = data
        self.calls = []

    async def inspect_remote_task(self, remote_task_id, *, model=None, metadata=None):
        self.calls.append((remote_task_id, model, dict(metadata or {})))
        return RemoteVideoInspection(
            RemoteVideoState.SUCCEEDED,
            "completed",
            output=VideoOutput(fmt="bytes", ext="mp4", data=self.data),
        )


class RemoteVideoRecoveryTests(unittest.TestCase):
    def _waiting_job(self, root, *, artifact_path=None):
        index = create_session_index(root)
        project = index.create(session_id="project-1")
        database = SQLiteDatabase(Path(root) / ".sceneforge" / "sceneforge.db")
        queue = SQLiteJobQueue(database)
        target = Path(artifact_path) if artifact_path else (
            index.working_dir(project["session_id"]) / "idea2video" / "scene_0" / "shots" / "0" / "video.mp4"
        )
        job = queue.enqueue(
            JobSpec(
                "resume-test",
                {"session_id": project["session_id"]},
                project_id=project["session_id"],
                concurrency_key=project["session_id"],
                idempotency_key=f"active:{project['session_id']}",
            )
        ).job
        queue.claim("dead-worker")
        queue.bind_remote_task(
            job.job_id,
            "remote-1",
            provider="veo_yunwu",
            model="veo-3",
            artifact_path=str(target),
            metadata={"scene_idx": 0, "shot_idx": 0},
        )
        return index, queue, queue.get(job.job_id), target

    def test_completed_remote_output_is_written_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = _CompletedProvider()
            index, queue, job, target = self._waiting_job(tmp)
            recovery = RemoteVideoRecovery(tmp, index, provider_factory=lambda current: provider)

            result = asyncio.run(recovery(job, JobContext(queue, job.job_id)))

            self.assertEqual(result.action, RemoteRecoveryAction.RETRY_WORKFLOW)
            self.assertEqual(target.read_bytes(), b"recovered-video")
            self.assertEqual(provider.calls[0][0], "remote-1")
            self.assertFalse(any(target.parent.glob("*.recovering")))

    def test_artifact_path_must_stay_inside_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "outside.mp4"
            provider = _CompletedProvider()
            index, queue, job, _ = self._waiting_job(tmp, artifact_path=outside)
            recovery = RemoteVideoRecovery(tmp, index, provider_factory=lambda current: provider)

            result = asyncio.run(recovery(job, JobContext(queue, job.job_id)))

            self.assertEqual(result.action, RemoteRecoveryAction.FAILED)
            self.assertIn("escapes", result.error)
            self.assertFalse(outside.exists())

    def test_worker_reconciles_then_retries_same_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = _CompletedProvider()
            index, queue, job, target = self._waiting_job(tmp)
            recovery = RemoteVideoRecovery(tmp, index, provider_factory=lambda current: provider)
            runner = DurableJobRunner(queue, remote_reconciler=recovery)

            async def resume(spec, context):
                self.assertTrue(target.exists())
                return {"ok": True, "session_id": spec.payload["session_id"]}

            runner.register_handler("resume-test", resume)
            runner.start()
            try:
                terminal = self._wait(runner, job.job_id)
                self.assertEqual(terminal["state"], "done")
                self.assertEqual(queue.get(job.job_id).attempt, 2)
                self.assertEqual(target.read_bytes(), b"recovered-video")
            finally:
                runner.stop()

    def test_canceled_remote_job_is_reconciled_without_retrying_handler(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = _CompletedProvider()
            index, queue, job, target = self._waiting_job(tmp)
            queue.request_cancel(job.job_id)
            recovery = RemoteVideoRecovery(tmp, index, provider_factory=lambda current: provider)
            runner = DurableJobRunner(queue, remote_reconciler=recovery)
            called = []

            async def resume(spec, context):
                called.append(True)
                return {"ok": True}

            runner.register_handler("resume-test", resume)
            runner.start()
            try:
                terminal = self._wait(runner, job.job_id)
                self.assertEqual(terminal["internal_state"], JobState.CANCELED.value)
                self.assertFalse(called)
                self.assertTrue(target.exists())
            finally:
                runner.stop()

    def test_recovery_thread_does_not_poll_current_workers_live_remote_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = create_session_index(tmp)
            index.create(session_id="live-project")
            queue = SQLiteJobQueue(SQLiteDatabase(Path(tmp) / ".sceneforge" / "sceneforge.db"))
            recovery_calls = []
            release = threading.Event()

            async def should_not_run(job, context):
                recovery_calls.append(job.job_id)
                return None

            runner = DurableJobRunner(queue, remote_reconciler=should_not_run)
            runner.worker.remote_poll_interval = 0.25

            async def live_handler(spec, context):
                context.event(
                    "video_task_created",
                    "submitted",
                    {
                        "provider": "veo_yunwu",
                        "model": "veo-3",
                        "task_id": "live-remote",
                        "artifact_path": str(index.working_dir("live-project") / "video.mp4"),
                    },
                )
                await asyncio.to_thread(release.wait)
                return {"ok": True}

            runner.register_handler("live", live_handler)
            runner.start()
            submitted = runner.submit_job(
                JobSpec("live", {}, project_id="live-project"),
                key="live-project",
            )
            try:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    if runner.get(submitted["job_id"])["internal_state"] == JobState.WAITING_PROVIDER.value:
                        break
                    time.sleep(0.02)
                time.sleep(0.6)
                self.assertFalse(recovery_calls)
            finally:
                release.set()
                self._wait(runner, submitted["job_id"])
                runner.stop()

    @staticmethod
    def _wait(runner, job_id, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = runner.get(job_id)
            if record and record["internal_state"] in {
                JobState.SUCCEEDED.value,
                JobState.CANCELED.value,
                JobState.FAILED.value,
            }:
                return record
            time.sleep(0.02)
        raise AssertionError("remote recovery did not reach a terminal state")
