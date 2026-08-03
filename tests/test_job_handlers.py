import tempfile
import unittest
from pathlib import Path

from domain.jobs import JobSpec, JobState
from infrastructure.sqlite import SQLiteDatabase, SQLiteJobQueue
from services.job_handlers import JobContext, JobHandlerRegistry


class JobHandlerRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_uses_serializable_job_type_and_persists_remote_handle(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = SQLiteJobQueue(SQLiteDatabase(Path(tmp) / "sceneforge.db"))
            spec = JobSpec("render", {"shot": 1})
            job = queue.enqueue(spec).job
            queue.claim("worker")
            registry = JobHandlerRegistry()

            async def render(received, context):
                self.assertEqual(received.payload["shot"], 1)
                context.bind_remote_task("remote-1")
                return {"ok": True}

            registry.register("render", render)
            self.assertTrue(registry.has("render"))
            self.assertFalse(registry.has("missing"))
            result = await registry.dispatch(spec, JobContext(queue, job.job_id))
            self.assertTrue(result["ok"])
            stored = queue.get(job.job_id)
            self.assertEqual(stored.state, JobState.WAITING_PROVIDER)
            self.assertEqual(stored.remote_task_id, "remote-1")

    async def test_unknown_handler_is_explicit(self):
        registry = JobHandlerRegistry()
        with self.assertRaises(KeyError):
            await registry.dispatch(JobSpec("unknown", {}), None)
