import asyncio
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent_runtime.models import ToolResult
from agent_runtime.session_index import SessionIndex
from infrastructure.sqlite import SQLiteDatabase, SQLiteJobQueue, SQLiteSessionStateStore
from services import DurableJobRunner, ProductionService
from services.production_metrics import aggregate_production_metrics


class _DurableFakeEngine:
    def __init__(self, index):
        self.index = index
        self.approve_release = threading.Event()

    async def start_topic(self, idea, progress=None, **kwargs):
        record = self.index.create(idea=idea, session_id="created-project")
        if progress:
            progress(
                "video_task_created",
                "provider accepted task",
                {"task_id": "remote-created", "shot_idx": 0, "shot_count": 1},
            )
        return {"ok": True, "session_id": record["session_id"], "stage": "script", "summary": "ready"}

    async def approve(self, session_id, progress=None):
        if progress:
            progress("approve_start", "started", {})
            await asyncio.to_thread(self.approve_release.wait)
            progress("approve_tick", "still working", {})
        return {"ok": True, "session_id": session_id, "stage": "storyboard", "summary": "ready"}

    async def revise(self, session_id, instruction, progress=None):
        return {"ok": True, "session_id": session_id, "stage": "script", "summary": instruction}

    async def resume_generation(self, session_id, progress=None):
        return {"ok": True, "session_id": session_id, "stage": "script", "summary": "resumed"}

    async def preview_keyframes(self, session_id, progress=None):
        if progress:
            progress("preview_keyframe_start", "building preview", {"count": 1})
        return {
            "ok": True,
            "session_id": session_id,
            "stage": "storyboard",
            "summary": "preview ready",
        }


class _DurableFakeAdapters:
    def __init__(self):
        self.calls = []

    async def sceneforge_regenerate_shot(self, args, runtime=None):
        self.calls.append(dict(args))
        artifact_path = args.get("artifact_path") or f"shot-{args['shot_idx']}.mp4"
        runtime.emit_progress(
            "provider accepted shot",
            stage="video_task_created",
            metadata={
                "provider": "veo_yunwu",
                "model": "veo-3",
                "task_id": "remote-shot-1",
                "artifact_path": artifact_path,
                "shot_idx": args["shot_idx"],
            },
        )
        return ToolResult(
            "sceneforge_regenerate_shot",
            True,
            "{}",
            {"session_id": args["session_id"], "shot_idx": args["shot_idx"]},
        )


class DurableProductionServiceTests(unittest.TestCase):
    def _wait(self, runner, job_id, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = runner.get(job_id)
            if record and record["state"] != "running":
                return record
            time.sleep(0.02)
        raise AssertionError("production job did not finish")

    def test_workflow_job_persists_progress_project_link_and_notification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = SQLiteDatabase(root / ".sceneforge" / "sceneforge.db")
            index = SessionIndex(root, state_store=SQLiteSessionStateStore(database))
            runner = DurableJobRunner(SQLiteJobQueue(database))
            notifications = []

            async def notify(text, target):
                notifications.append((target, text))

            service = ProductionService(_DurableFakeEngine(index), runner, notifier=notify)
            try:
                submitted = service.start_topic("离别", target="user-1")
                completed = self._wait(runner, submitted["job_id"])
                self.assertEqual(completed["state"], "done")
                self.assertEqual(completed["result"]["session_id"], "created-project")
                self.assertEqual(completed["progress"][0]["stage"], "video_task_created")
                stored = runner.queue.get(submitted["job_id"])
                self.assertEqual(stored.spec.project_id, "created-project")
                self.assertEqual(stored.remote_task_id, "remote-created")
                self.assertEqual(runner.last_job("created-project")["job_id"], submitted["job_id"])
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not notifications:
                    time.sleep(0.01)
                self.assertTrue(any(target == "user-1" and "script" in text for target, text in notifications))
            finally:
                runner.stop()

    def test_preview_keyframes_handler_is_registered_before_worker_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = SQLiteDatabase(root / ".sceneforge" / "sceneforge.db")
            index = SessionIndex(root, state_store=SQLiteSessionStateStore(database))
            index.create(session_id="preview-project")
            runner = DurableJobRunner(SQLiteJobQueue(database))
            service = ProductionService(_DurableFakeEngine(index), runner)
            try:
                submitted = service.preview_keyframes("preview-project")
                completed = self._wait(runner, submitted["job_id"])
                stored = runner.queue.get(submitted["job_id"])

                self.assertEqual(completed["state"], "done")
                self.assertEqual(completed["result"]["summary"], "preview ready")
                self.assertEqual(completed["progress"][0]["stage"], "preview_keyframe_start")
                self.assertEqual(stored.spec.job_type, "workflow.preview_keyframes")
            finally:
                runner.stop()

    def test_running_job_cancels_at_next_progress_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = SQLiteDatabase(root / ".sceneforge" / "sceneforge.db")
            index = SessionIndex(root, state_store=SQLiteSessionStateStore(database))
            index.create(session_id="existing")
            runner = DurableJobRunner(SQLiteJobQueue(database))
            engine = _DurableFakeEngine(index)
            service = ProductionService(engine, runner)
            try:
                submitted = service.approve("existing")
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    running = runner.get(submitted["job_id"])
                    if running and running["progress"]:
                        break
                    time.sleep(0.01)
                canceled = service.cancel_job(submitted["job_id"])
                self.assertTrue(canceled["ok"])
                engine.approve_release.set()
                terminal = self._wait(runner, submitted["job_id"])
                self.assertEqual(terminal["internal_state"], "canceled")
                self.assertEqual(terminal["state"], "failed")

                continued = service.continue_cancelled("existing")
                self.assertTrue(continued["accepted"])
                completed = self._wait(runner, continued["job_id"])
                self.assertEqual(completed["state"], "done")
                self.assertEqual(completed["result"]["stage"], "storyboard")
            finally:
                engine.approve_release.set()
                runner.stop()

    def test_regenerate_shot_bridges_tool_progress_to_durable_remote_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = SQLiteDatabase(root / ".sceneforge" / "sceneforge.db")
            index = SessionIndex(root, state_store=SQLiteSessionStateStore(database))
            index.create(session_id="existing")
            runner = DurableJobRunner(SQLiteJobQueue(database))
            service = ProductionService(
                _DurableFakeEngine(index),
                runner,
                adapters=_DurableFakeAdapters(),
            )
            target = index.working_dir("existing") / "shots" / "2" / "video.mp4"
            try:
                submitted = service.regenerate_shot(
                    "existing",
                    2,
                    description={"artifact_path": str(target)},
                )
                completed = self._wait(runner, submitted["job_id"])
                self.assertEqual(completed["state"], "done")
                stored = runner.queue.get(submitted["job_id"])
                self.assertEqual(stored.remote_task_id, "remote-shot-1")
                self.assertEqual(stored.remote_provider, "veo_yunwu")
                self.assertEqual(stored.remote_artifact_path, str(target))
            finally:
                runner.stop()

    def test_batch_regeneration_runs_as_one_durable_job_with_locks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = SQLiteDatabase(root / ".sceneforge" / "sceneforge.db")
            index = SessionIndex(root, state_store=SQLiteSessionStateStore(database))
            index.create(session_id="batch-project")
            runner = DurableJobRunner(SQLiteJobQueue(database))
            adapters = _DurableFakeAdapters()
            service = ProductionService(
                _DurableFakeEngine(index), runner, adapters=adapters
            )
            service.batch_regeneration_impact = lambda *_args, **_kwargs: {
                "requested_shots": [
                    {"scene_index": 0, "shot_idx": 0},
                    {"scene_index": 0, "shot_idx": 2},
                ],
                "execution_roots": [
                    {"scene_index": 0, "shot_idx": 0},
                    {"scene_index": 0, "shot_idx": 2},
                ],
                "affected_shots": [
                    {"scene_index": 0, "shot_idx": 0},
                    {"scene_index": 0, "shot_idx": 2},
                ],
                "locked_dimensions": ["composition", "identity"],
                "savings_estimate": {
                    "avoided_shot_count": 4,
                    "estimated_generation_seconds_saved": 40.0,
                    "estimated_cost_saved_lower_bound": 8.0,
                    "estimated_cost_saved_upper_bound": 24.0,
                    "full_rerender_cost_estimate": {"currency": "CNY"},
                },
            }
            try:
                submitted = service.regenerate_shots(
                    "batch-project",
                    [
                        {"scene_index": 0, "shot_idx": 0},
                        {"scene_index": 0, "shot_idx": 2},
                    ],
                    reason="continuity",
                    dimensions=["visual"],
                    locked_dimensions=["identity", "composition"],
                )
                completed = self._wait(runner, submitted["job_id"])

                self.assertEqual(completed["state"], "done")
                self.assertTrue(completed["result"]["ok"])
                self.assertEqual(completed["result"]["execution_count"], 2)
                self.assertEqual([call["shot_idx"] for call in adapters.calls], [0, 2])
                self.assertEqual(
                    adapters.calls[0]["locked_dimensions"],
                    ["composition", "identity"],
                )
                savings = aggregate_production_metrics(
                    index.working_dir("batch-project")
                )["summary"]["local_rework_savings"]
                self.assertEqual(savings["completed_batches"], 1)
                self.assertEqual(savings["avoided_shot_count"], 4)
                self.assertEqual(savings["estimated_cost_saved_upper_bound"], 24.0)
                self.assertTrue(any(
                    item["stage"] == "batch_shot_start"
                    for item in completed["progress"]
                ))
            finally:
                runner.stop()

    def test_timeline_render_runs_as_durable_job_and_records_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = SQLiteDatabase(root / ".sceneforge" / "sceneforge.db")
            index = SessionIndex(root, state_store=SQLiteSessionStateStore(database))
            index.create(session_id="timeline-project")
            runner = DurableJobRunner(SQLiteJobQueue(database))
            service = ProductionService(
                _DurableFakeEngine(index), runner, adapters=_DurableFakeAdapters()
            )

            class TimelineStub:
                def __init__(self):
                    self.rendered = []

                def save_plan(self, plan):
                    return {**plan, "output_duration": 8.0}

                def output_fingerprint(self):
                    return "source-v1"

                def render(self, plan):
                    self.rendered.append(plan)
                    return {
                        "ok": True,
                        "clip_count": len(plan.get("clips") or []),
                        "output_duration": 8.0,
                        "archive_path": "archive/v1",
                    }

            timeline = TimelineStub()
            service._timeline_editor = lambda _sid: timeline
            try:
                submitted = service.render_edit_plan(
                    "timeline-project",
                    {"clips": [{"clip_id": "0_0"}, {"clip_id": "0_1"}]},
                )
                completed = self._wait(runner, submitted["job_id"])

                self.assertEqual(completed["state"], "done")
                self.assertEqual(completed["result"]["clip_count"], 2)
                self.assertEqual(len(timeline.rendered), 1)
                events_path = (
                    index.working_dir("timeline-project")
                    / ".sceneforge"
                    / "production_events.jsonl"
                )
                events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
                self.assertEqual(events[-1]["event_type"], "timeline_rendered")
                self.assertEqual(events[-1]["metadata"]["output_duration"], 8.0)
            finally:
                runner.stop()
