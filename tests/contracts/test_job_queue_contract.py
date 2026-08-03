import tempfile
import threading
import unittest
from pathlib import Path

from domain.jobs import JobSpec, JobState, JobTransitionError
from infrastructure.sqlite import SQLiteDatabase, SQLiteJobQueue
from infrastructure.sqlite.job_queue import WorkerLeaseLostError
from repositories.job_queue import JobQueue


class SQLiteJobQueueContractTests(unittest.TestCase):
    def _queue(self, root):
        return SQLiteJobQueue(SQLiteDatabase(Path(root) / "sceneforge.db"))

    def test_enqueue_claim_progress_and_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = self._queue(tmp)
            self.assertIsInstance(queue, JobQueue)
            enqueued = queue.enqueue(JobSpec("render_shot", {"shot_id": "s1"}, idempotency_key="render:s1:v1"))
            self.assertTrue(enqueued.accepted)
            self.assertEqual(enqueued.job.state, JobState.QUEUED)

            claimed = queue.claim("worker-1")
            self.assertEqual(claimed.job_id, enqueued.job.job_id)
            self.assertEqual(claimed.state, JobState.RUNNING)
            self.assertEqual(claimed.attempt, 1)
            queue.update_progress(claimed.job_id, 2, 5)

            reopened = self._queue(tmp).get(claimed.job_id)
            self.assertEqual((reopened.progress_current, reopened.progress_total), (2, 5))

    def test_idempotency_blocks_only_active_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = self._queue(tmp)
            first = queue.enqueue(JobSpec("render", {}, idempotency_key="same"))
            duplicate = queue.enqueue(JobSpec("render", {"new": True}, idempotency_key="same"))
            self.assertFalse(duplicate.accepted)
            self.assertEqual(duplicate.reason, "duplicate")
            self.assertEqual(duplicate.job.job_id, first.job.job_id)

            queue.claim("worker")
            queue.transition(first.job.job_id, JobState.SUCCEEDED, result={"path": "clip.mp4"})
            replacement = queue.enqueue(JobSpec("render", {}, idempotency_key="same"))
            self.assertTrue(replacement.accepted)

    def test_remote_handle_survives_restart_for_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = self._queue(tmp)
            job = queue.enqueue(JobSpec("render", {})).job
            queue.claim("worker")
            waiting = queue.bind_remote_task(
                job.job_id,
                "remote-42",
                provider="veo_yunwu",
                model="veo-3",
                artifact_path="shots/0/video.mp4",
                metadata={"shot_idx": 0},
            )
            self.assertEqual(waiting.remote_task_id, "remote-42")
            self.assertEqual(waiting.remote_provider, "veo_yunwu")

            reopened = self._queue(tmp)
            self.assertEqual(reopened.recover_interrupted(), 0)
            recovered = reopened.get(job.job_id)
            self.assertEqual(recovered.state, JobState.WAITING_PROVIDER)
            self.assertEqual(recovered.remote_task_id, "remote-42")
            self.assertEqual(recovered.remote_metadata["shot_idx"], 0)
            self.assertEqual(reopened.list_remote_active()[0].job_id, job.job_id)

    def test_incomplete_legacy_remote_context_becomes_interrupted(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = self._queue(tmp)
            job = queue.enqueue(JobSpec("render", {})).job
            queue.claim("old-worker")
            queue.transition(
                job.job_id,
                JobState.WAITING_PROVIDER,
                remote_task_id="legacy-without-provider",
            )

            self.assertEqual(queue.recover_interrupted(), 1)
            self.assertEqual(queue.get(job.job_id).state, JobState.INTERRUPTED)

    def test_cancel_and_terminal_transition_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = self._queue(tmp)
            job = queue.enqueue(JobSpec("render", {})).job
            canceling = queue.request_cancel(job.job_id)
            self.assertEqual(canceling.state, JobState.CANCELED)
            with self.assertRaises(JobTransitionError):
                queue.transition(job.job_id, JobState.RUNNING)

    def test_payload_must_be_serializable(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = self._queue(tmp)
            with self.assertRaises(ValueError):
                queue.enqueue(JobSpec("render", {"bad": object()}))

    def test_concurrent_workers_claim_each_job_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = self._queue(tmp)
            expected = {queue.enqueue(JobSpec("render", {"index": index})).job.job_id for index in range(12)}
            claimed: list[str] = []
            claimed_lock = threading.Lock()

            def worker(worker_id):
                local_queue = self._queue(tmp)
                while True:
                    job = local_queue.claim(worker_id)
                    if job is None:
                        return
                    with claimed_lock:
                        claimed.append(job.job_id)

            workers = [threading.Thread(target=worker, args=(f"worker-{index}",)) for index in range(4)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()

            self.assertEqual(set(claimed), expected)
            self.assertEqual(len(claimed), len(expected))

    def test_max_attempts_blocks_another_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = self._queue(tmp)
            job = queue.enqueue(JobSpec("render", {}, max_attempts=1)).job
            queue.claim("worker")
            with self.assertRaises(ValueError):
                queue.transition(job.job_id, JobState.RETRY_WAIT)

    def test_expired_worker_cannot_claim_or_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = self._queue(tmp)
            self.assertTrue(queue.acquire_worker_lease("old-owner", ttl_seconds=30))
            running = queue.enqueue(JobSpec("render", {})).job
            queue.claim("old-worker", lease_owner_id="old-owner")
            queued = queue.enqueue(JobSpec("render", {"next": True})).job

            with queue.database.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE worker_leases SET expires_at = 0 WHERE owner_id = ?",
                    ("old-owner",),
                )
            self.assertTrue(queue.acquire_worker_lease("new-owner", ttl_seconds=300))
            with self.assertRaises(WorkerLeaseLostError):
                queue.claim("old-worker", lease_owner_id="old-owner")
            with self.assertRaises(WorkerLeaseLostError):
                queue.transition(
                    running.job_id,
                    JobState.SUCCEEDED,
                    result={"ok": True},
                    lease_owner_id="old-owner",
                )

            claimed = queue.claim("new-worker", lease_owner_id="new-owner")
            self.assertEqual(claimed.job_id, queued.job_id)

    def test_cancel_requested_is_finalized_during_restart_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = self._queue(tmp)
            job = queue.enqueue(JobSpec("render", {}, idempotency_key="active:project")).job
            queue.claim("worker")
            canceling = queue.request_cancel(job.job_id)
            self.assertEqual(canceling.state, JobState.CANCEL_REQUESTED)

            self.assertEqual(queue.recover_interrupted(), 1)
            canceled = queue.get(job.job_id)
            self.assertEqual(canceled.state, JobState.CANCELED)
            self.assertIsNotNone(canceled.finished_at)
            replacement = queue.enqueue(JobSpec("render", {}, idempotency_key="active:project"))
            self.assertTrue(replacement.accepted)
