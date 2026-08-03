import tempfile
import unittest

from agent_runtime.session_index import SessionIndex
from services.stage_handlers import StageHandlerRegistry
from services.workflow_engine import WorkflowEngine, _split_script_into_scenes


class RecordingHandler:
    def __init__(self, stage: str, result: str = "handled"):
        self.stage = stage
        self.result = result
        self.calls = []

    async def run(self, engine, session, instruction="", progress=None):
        self.calls.append((engine, session, instruction, progress))
        return self.result


class TestStageHandlerRegistry(unittest.IsolatedAsyncioTestCase):
    def test_default_registry_has_every_workflow_gate(self):
        registry = StageHandlerRegistry.default()
        self.assertEqual(
            set(registry.stages),
            {"script", "storyboard", "shot_video", "final"},
        )

    def test_duplicate_and_unknown_handlers_fail_explicitly(self):
        registry = StageHandlerRegistry((RecordingHandler("script"),))
        with self.assertRaisesRegex(ValueError, "already registered"):
            registry.register(RecordingHandler("script"))
        with self.assertRaisesRegex(ValueError, "Unknown stage handler"):
            registry.get("missing")

    def test_handler_can_be_replaced_independently(self):
        registry = StageHandlerRegistry.default()
        replacement = RecordingHandler("storyboard")
        registry.register(replacement, replace=True)
        self.assertIs(registry.get("storyboard"), replacement)
        self.assertEqual(registry.get("script").stage, "script")

    async def test_workflow_delegates_to_injected_handler(self):
        handler = RecordingHandler("script", "custom output")
        registry = StageHandlerRegistry((handler,))
        with tempfile.TemporaryDirectory() as tmp:
            engine = WorkflowEngine(
                SessionIndex(tmp),
                tmp,
                stage_handlers=registry,
            )
            session = {"session_id": "s1"}
            result = await engine._run_gate("script", session, "make it quieter")

        self.assertEqual(result, "custom output")
        self.assertEqual(handler.calls[0][1], session)
        self.assertEqual(handler.calls[0][2], "make it quieter")

    async def test_final_stage_remains_replaceable(self):
        handler = RecordingHandler("final", "cloud final ready")
        registry = StageHandlerRegistry((handler,))
        with tempfile.TemporaryDirectory() as tmp:
            engine = WorkflowEngine(
                SessionIndex(tmp),
                tmp,
                stage_handlers=registry,
            )
            result = await engine._run_gate("final", {"session_id": "s1"})

        self.assertEqual(result, "cloud final ready")


class TestScriptSceneSplitCompatibility(unittest.TestCase):
    def test_legacy_import_still_splits_scene_headers(self):
        script = "片名：归途\n场景1 山门\n甲：等我。\n场景2 长街\n乙转身。"
        scenes = _split_script_into_scenes(script)
        self.assertEqual(len(scenes), 2)
        self.assertTrue(scenes[0].startswith("片名：归途\n场景1"))
        self.assertTrue(scenes[1].startswith("场景2"))
