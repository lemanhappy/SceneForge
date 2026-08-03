import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path

from domain.jobs import JobSpec
from infrastructure.sqlite import SQLiteDatabase, SQLiteJobQueue
from services.durable_job_runner import DurableJobRunner


class DurableJobRunnerTests(unittest.TestCase):
    def _components(self, root):
        queue = SQLiteJobQueue(SQLiteDatabase(Path(root) / "sceneforge.db"))
        return queue, DurableJobRunner(queue)

    def _wait(self, runner, job_id, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = runner.get(job_id)
            if record and record["state"] != "running":
                return record
            time.sleep(0.02)
        raise AssertionError("durable job did not finish")

    def test_queued_job_survives_runner_recreation(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, _ = self._components(tmp)
            queued = queue.enqueue(JobSpec("echo", {"value": 7})).job

            reopened_queue, runner = self._components(tmp)

            async def echo(spec, context):
                context.event("working", "processing", {"count": 1})
                return {"value": spec.payload["value"]}

            runner.register_handler("echo", echo)
            runner.start()
            try:
                result = self._wait(runner, queued.job_id)
                self.assertEqual(result["state"], "done")
                self.assertEqual(result["result"], {"value": 7})
                self.assertEqual(result["progress"][0]["message"], "processing")
                self.assertEqual(reopened_queue.get(queued.job_id).state.value, "succeeded")
            finally:
                runner.stop()

    def test_recent_jobs_expose_task_center_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, runner = self._components(tmp)
            job = queue.enqueue(JobSpec(
                "workflow.preview_keyframes",
                {"session_id": "project-1"},
            )).job

            recent = runner.list_recent()

            self.assertEqual(recent[0]["job_id"], job.job_id)
            self.assertEqual(recent[0]["project_id"], "project-1")
            self.assertEqual(recent[0]["job_type"], "workflow.preview_keyframes")
            self.assertTrue(recent[0]["created_at"])

    def test_single_flight_key_and_remote_handle(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, runner = self._components(tmp)
            release = threading.Event()

            async def render(spec, context):
                context.event("video_task_created", "submitted", {"task_id": "remote-9", "shot_idx": 0})
                await asyncio.to_thread(release.wait)
                return {"ok": True}

            runner.register_handler("render", render)
            runner.start()
            try:
                first = runner.submit_job(JobSpec("render", {}), key="project-1")
                self.assertTrue(first["accepted"])
                duplicate = runner.submit_job(JobSpec("render", {"other": True}), key="project-1")
                self.assertFalse(duplicate["accepted"])
                self.assertEqual(duplicate["state"], "busy")
                release.set()
                completed = self._wait(runner, first["job_id"])
                self.assertEqual(completed["state"], "done")
                self.assertNotIn("task_id", completed["progress"][0].get("meta", {}))
                self.assertEqual(runner.queue.get(first["job_id"]).remote_task_id, "remote-9")
            finally:
                release.set()
                runner.stop()

    def test_running_job_is_marked_interrupted_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, _ = self._components(tmp)
            job = queue.enqueue(JobSpec("render", {})).job
            queue.claim("old-worker")

            _, runner = self._components(tmp)
            runner.register_handler("render", lambda spec, context: None)
            runner.start()
            try:
                record = runner.get(job.job_id)
                self.assertEqual(record["state"], "failed")
                self.assertEqual(record["internal_state"], "interrupted")
                self.assertIn("Interrupted", record["error"])
            finally:
                runner.stop()

    def test_handler_failure_is_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, runner = self._components(tmp)

            async def fail(spec, context):
                raise RuntimeError("provider unavailable")

            runner.register_handler("fail", fail)
            runner.start()
            try:
                submitted = runner.submit_job(JobSpec("fail", {}))
                failed = self._wait(runner, submitted["job_id"])
                self.assertEqual(failed["state"], "failed")
                self.assertIn("provider unavailable", failed["error"])
            finally:
                runner.stop()

    def test_second_runner_does_not_interrupt_live_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, first = self._components(tmp)
            release = threading.Event()

            async def slow(spec, context):
                await asyncio.to_thread(release.wait)
                return {"ok": True}

            first.register_handler("slow", slow)
            first.start()
            submitted = first.submit_job(JobSpec("slow", {}), key="shared")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if first.get(submitted["job_id"])["internal_state"] == "running":
                    break
                time.sleep(0.01)

            _, second = self._components(tmp)
            second.register_handler("slow", slow)
            second.start()
            try:
                self.assertEqual(first.get(submitted["job_id"])["internal_state"], "running")
                self.assertFalse(second.worker._owns_lease)
            finally:
                release.set()
                self._wait(first, submitted["job_id"])
                first.stop()
                second.stop()

    def test_interrupted_key_can_submit_a_new_resume_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, _ = self._components(tmp)
            old = queue.enqueue(
                JobSpec(
                    "render",
                    {},
                    concurrency_key="project-1",
                    idempotency_key="active:project-1",
                )
            ).job
            queue.claim("dead-worker")
            queue.recover_interrupted()
            self.assertEqual(queue.get(old.job_id).state.value, "interrupted")

            _, runner = self._components(tmp)

            async def resume(spec, context):
                return {"ok": True}

            runner.register_handler("resume", resume)
            replacement = runner.submit_job(JobSpec("resume", {}), key="project-1")
            self.assertTrue(replacement["accepted"])

    def test_standby_runner_takes_over_after_stale_lease_expires(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, runner = self._components(tmp)
            queue.acquire_worker_lease("dead-process", ttl_seconds=300)
            runner.worker.lease_ttl = 30

            async def echo(spec, context):
                return {"ok": True}

            runner.register_handler("echo", echo)
            runner.start()
            submitted = runner.submit_job(JobSpec("echo", {}))
            with queue.database.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE worker_leases SET expires_at = 0 WHERE owner_id = ?",
                    ("dead-process",),
                )
            try:
                completed = self._wait(runner, submitted["job_id"], timeout=30.0)
                self.assertEqual(completed["state"], "done")
                self.assertTrue(runner.worker._owns_lease)
            finally:
                runner.stop()

    def test_heartbeat_failure_stops_old_worker_before_standby_takeover(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, first = self._components(tmp)
            first.worker.lease_ttl = 0.5

            def fail_renewal(*args, **kwargs):
                raise RuntimeError("database unavailable")

            queue.renew_worker_lease = fail_renewal

            async def echo(spec, context):
                return {"ok": True}

            first.register_handler("echo", echo)
            first.start()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not first.worker._stop_event.is_set():
                time.sleep(0.02)
            self.assertTrue(first.worker._stop_event.is_set())

            submitted = first.submit_job(JobSpec("echo", {}))
            _, second = self._components(tmp)
            second.worker.lease_ttl = 0.5
            second.register_handler("echo", echo)
            second.start()
            try:
                completed = self._wait(second, submitted["job_id"], timeout=4.0)
                self.assertEqual(completed["state"], "done")
                self.assertTrue(second.worker._owns_lease)
            finally:
                first.stop()
                second.stop()
