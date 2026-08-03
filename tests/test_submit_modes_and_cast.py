"""Tests for submit modes (主题/剧本) + explicit cast selection (多角色).

Covers the pure script splitter, the engine's registry/cast-brief helpers
(explicit selection works even with character_assets disabled), and the
production API topic handler's mode/script/cast validation + forwarding.
"""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

import yaml

from services.workflow_engine import WorkflowEngine, _split_script_into_scenes
from server.production_api import ProductionAPI


class TestSplit(unittest.TestCase):
    def test_no_headers_single_scene(self):
        self.assertEqual(_split_script_into_scenes("一段没有任何场景标记的剧本。"),
                         ["一段没有任何场景标记的剧本。"])

    def test_empty(self):
        self.assertEqual(_split_script_into_scenes("  "), [])

    def test_chinese_headers(self):
        txt = "场景一 咖啡馆\n王云宝：回来了。\n\n场景二 公司\n林总：你敢？"
        scenes = _split_script_into_scenes(txt)
        self.assertEqual(len(scenes), 2)
        self.assertTrue(scenes[0].startswith("场景一"))
        self.assertIn("林总", scenes[1])

    def test_int_ext_headers_and_preamble(self):
        txt = "标题：逆袭\nINT. OFFICE - DAY\n动作A\nEXT. STREET - NIGHT\n动作B"
        scenes = _split_script_into_scenes(txt)
        self.assertEqual(len(scenes), 2)
        self.assertIn("标题：逆袭", scenes[0])  # preamble folded into first scene
        self.assertIn("INT. OFFICE", scenes[0])
        self.assertIn("EXT. STREET", scenes[1])


def _engine_with_registry(workspace: Path, *, enabled: bool, assets: dict):
    (workspace / "configs").mkdir(parents=True, exist_ok=True)
    (workspace / "assets" / "characters").mkdir(parents=True, exist_ok=True)
    reg_path = "assets/characters/registry.yaml"
    (workspace / reg_path).write_text(yaml.safe_dump({"characters": assets}, allow_unicode=True), encoding="utf-8")
    cfg = {"character_assets": {"enabled": enabled, "registry_path": reg_path}}
    (workspace / "configs" / "idea2video.yaml").write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    # session_index unused by the helpers under test
    return WorkflowEngine(session_index=None, workspace_root=str(workspace))


_ASSETS = {
    "wang": {"display_name": "王云宝", "description": "隐忍的天才程序员", "type": "reference_images", "assets": {}},
    "lin": {"display_name": "林总", "description": "傲慢的反派", "type": "reference_images", "assets": {}},
}


class TestEngineHelpers(unittest.TestCase):
    def test_registry_for_explicit_selection_even_when_disabled(self):
        with tempfile.TemporaryDirectory() as d:
            eng = _engine_with_registry(Path(d), enabled=False, assets=_ASSETS)
            # no selection + disabled -> no registry (upstream behaviour)
            self.assertIsNone(eng._registry_for({"character_asset_ids": []}))
            # explicit selection -> registry narrowed to just the picked assets
            reg = eng._registry_for({"character_asset_ids": ["wang"]})
            self.assertIsNotNone(reg)
            self.assertEqual(len(reg.all()), 1)
            self.assertEqual(reg.all()[0].display_name, "王云宝")

    def test_cast_brief_lists_selected_names_and_descs(self):
        with tempfile.TemporaryDirectory() as d:
            eng = _engine_with_registry(Path(d), enabled=False, assets=_ASSETS)
            brief = eng._cast_brief({"character_asset_ids": ["wang", "lin"]})
            self.assertIn("王云宝", brief)
            self.assertIn("隐忍的天才程序员", brief)
            self.assertIn("林总", brief)
            self.assertIn("不要改名", brief)

    def test_cast_brief_empty_when_no_selection(self):
        with tempfile.TemporaryDirectory() as d:
            eng = _engine_with_registry(Path(d), enabled=False, assets=_ASSETS)
            self.assertEqual(eng._cast_brief({"character_asset_ids": []}), "")

    def test_augment_requirement_includes_cast(self):
        with tempfile.TemporaryDirectory() as d:
            eng = _engine_with_registry(Path(d), enabled=False, assets=_ASSETS)
            session = {"user_requirement": "前3秒强钩子", "character_asset_ids": ["wang"]}
            out = eng._augment_requirement(session, "")
            self.assertIn("前3秒强钩子", out)
            self.assertIn("王云宝", out)


class _FakeService:
    def __init__(self):
        self.kwargs = None

    def start_topic(self, idea, **kw):
        self.kwargs = {"idea": idea, **kw}
        return {"job_id": "j1"}


class TestTopicApi(unittest.TestCase):
    def _api(self):
        svc = _FakeService()
        return ProductionAPI(session_index=None, service=svc, adapters=None), svc

    def test_idea_mode_requires_idea(self):
        api, _ = self._api()
        st, _b = asyncio.run(api.handle("POST", "/api/production/topic", {"mode": "idea", "idea": ""}))
        self.assertEqual(st, 400)

    def test_script_mode_requires_script(self):
        api, _ = self._api()
        st, _b = asyncio.run(api.handle("POST", "/api/production/topic", {"mode": "script", "script": ""}))
        self.assertEqual(st, 400)

    def test_script_mode_forwards_fields(self):
        api, svc = self._api()
        st, _b = asyncio.run(api.handle("POST", "/api/production/topic",
                                        {"mode": "script", "script": "场景一 …", "idea": "标题",
                                         "character_asset_ids": ["wang", "lin", ""]}))
        self.assertEqual(st, 200)
        self.assertEqual(svc.kwargs["mode"], "script")
        self.assertEqual(svc.kwargs["script"], "场景一 …")
        self.assertEqual(svc.kwargs["character_asset_ids"], ["wang", "lin"])  # blanks dropped

    def test_idea_mode_forwards_cast(self):
        api, svc = self._api()
        st, _b = asyncio.run(api.handle("POST", "/api/production/topic",
                                        {"idea": "一个程序员的逆袭", "character_asset_ids": ["wang"]}))
        self.assertEqual(st, 200)
        self.assertEqual(svc.kwargs["mode"], "idea")
        self.assertEqual(svc.kwargs["character_asset_ids"], ["wang"])

    def test_quality_tier_is_validated_and_forwarded(self):
        api, svc = self._api()
        status, _ = asyncio.run(api.handle("POST", "/api/production/topic", {
            "idea": "一个程序员的逆袭", "quality_tier": "quality",
        }))
        self.assertEqual(status, 200)
        self.assertEqual(svc.kwargs["quality_tier"], "quality")
        status, _ = asyncio.run(api.handle("POST", "/api/production/topic", {
            "idea": "一个程序员的逆袭", "quality_tier": "unknown",
        }))
        self.assertEqual(status, 400)

    def test_subtitle_controls_are_forwarded_as_project_overrides(self):
        api, svc = self._api()
        status, _ = asyncio.run(api.handle("POST", "/api/production/topic", {
            "idea": "一个程序员的逆袭",
            "subtitle_enabled": True,
            "subtitle_burn_in": True,
        }))
        self.assertEqual(status, 200)
        self.assertEqual(svc.kwargs["overrides"]["subtitle_enabled"], True)
        self.assertEqual(svc.kwargs["overrides"]["subtitle_burn_in"], True)

    def test_reusable_asset_selections_are_forwarded(self):
        api, svc = self._api()
        status, _ = asyncio.run(api.handle("POST", "/api/production/topic", {
            "idea": "古装离别",
            "prop_asset_ids": ["hero_sword", ""],
            "scene_asset_ids": ["city_gate"],
        }))
        self.assertEqual(status, 200)
        self.assertEqual(svc.kwargs["prop_asset_ids"], ["hero_sword"])
        self.assertEqual(svc.kwargs["scene_asset_ids"], ["city_gate"])

    def test_continuity_source_is_validated_and_forwarded(self):
        svc = _FakeService()

        class _Index:
            @staticmethod
            def get(session_id):
                return {"session_id": session_id} if session_id == "episode-1" else None

        api = ProductionAPI(_Index(), svc, adapters=None)
        status, _ = asyncio.run(api.handle("POST", "/api/production/topic", {
            "idea": "第二集",
            "continuity_source_session_id": "episode-1",
        }))
        self.assertEqual(status, 200)
        self.assertEqual(svc.kwargs["continuity_source_session_id"], "episode-1")

        status, body = asyncio.run(api.handle("POST", "/api/production/topic", {
            "idea": "第二集",
            "continuity_source_session_id": "missing",
        }))
        self.assertEqual(status, 400)
        self.assertIn("continuity source", body["error"])


class _LastJobRunner:
    def __init__(self, handler_available):
        self.handler_available = handler_available

    def last_job(self, _session_id):
        return {
            "state": "failed",
            "job_type": "workflow.preview_keyframes",
            "error": "KeyError: 'no handler registered for job_type: workflow.preview_keyframes'",
            "result": None,
        }

    def has_handler(self, job_type):
        return self.handler_available and job_type == "workflow.preview_keyframes"


class TestLastErrorCompatibility(unittest.TestCase):
    def _api(self, handler_available):
        service = type("Service", (), {"runner": _LastJobRunner(handler_available)})()
        return ProductionAPI(session_index=None, service=service, adapters=None)

    def test_obsolete_missing_handler_error_clears_after_upgrade(self):
        self.assertIsNone(self._api(True)._last_error("project", busy=False))

    def test_missing_handler_error_remains_when_still_unresolved(self):
        error = self._api(False)._last_error("project", busy=False)
        self.assertIn("no handler registered", error["note"])


if __name__ == "__main__":
    unittest.main()
