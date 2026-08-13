"""Tests for inbound authorization, rate limiting, and TriggerService routing."""

import asyncio
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent_runtime.models import ToolResult
from agent_runtime.session_index import SessionIndex
from commands import UserCommand, parse_user_command
from services import Authorizer, BudgetGuard, InboundRateLimiter, JobRunner, ProductionService, TriggerService, WorkflowEngine
from services.stage_handlers import StageHandlerRegistry


class TestAuthorizer(unittest.TestCase):
    def test_disabled_when_unconfigured_allows_all(self):
        auth = Authorizer.from_config({})
        self.assertFalse(auth.enabled)
        self.assertTrue(auth.is_authorized("feishu", "anyone"))

    def test_enabled_restricts_to_allowlist(self):
        auth = Authorizer.from_config({"security": {"authorized_sources": [{"channel": "feishu", "user_id": "ou_1"}]}})
        self.assertTrue(auth.enabled)
        self.assertTrue(auth.is_authorized("feishu", "ou_1"))
        self.assertFalse(auth.is_authorized("feishu", "ou_2"))
        self.assertFalse(auth.is_authorized("wechat", "ou_1"))


class TestInboundRateLimiter(unittest.TestCase):
    def test_no_limit_allows_all(self):
        rl = InboundRateLimiter.from_config({})
        for _ in range(100):
            self.assertTrue(rl.allow("u"))

    def test_daily_cap_rejects_over_limit(self):
        day = ["2026-06-20"]
        rl = InboundRateLimiter(max_requests_per_day=2, day_provider=lambda: day[0])
        self.assertTrue(rl.allow("u"))
        self.assertTrue(rl.allow("u"))
        self.assertFalse(rl.allow("u"))      # over cap
        self.assertTrue(rl.allow("other"))   # per-user
        day[0] = "2026-06-21"                 # new day resets
        self.assertTrue(rl.allow("u"))


class FakeAdapters:
    def __init__(self):
        self.calls = []

    async def sceneforge_narrative_planning(self, args):
        self.calls.append(("plan", args))
        return ToolResult("sceneforge_narrative_planning", True, "{}", {"session_id": "s", "idea": args.get("idea")})

    async def sceneforge_regenerate_shot(self, args):
        self.calls.append(("regen", args))
        return ToolResult("sceneforge_regenerate_shot", True, "{}", {"shot_idx": args["shot_idx"]})

    async def sceneforge_publish(self, args):
        self.calls.append(("publish", args))
        return ToolResult("sceneforge_publish", True, "{}", {"url": "https://x/v.mp4"})


class TestTriggerService(unittest.IsolatedAsyncioTestCase):
    def _service(self, tmp, authorizer=None, rate_limiter=None):
        index = SessionIndex(tmp)
        adapters = FakeAdapters()
        return TriggerService(index, adapters, authorizer=authorizer, rate_limiter=rate_limiter), index, adapters

    async def test_unauthorized_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth = Authorizer({("feishu", "ou_ok")})
            svc, _, adapters = self._service(tmp, authorizer=auth)
            res = await svc.handle_command(parse_user_command("做个短片", source="feishu"), sender_id="ou_bad")
            self.assertFalse(res["ok"])
            self.assertEqual(res["reason"], "unauthorized")
            self.assertEqual(adapters.calls, [])

    async def test_rate_limited_blocks_generation_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            rl = InboundRateLimiter(max_requests_per_day=1, day_provider=lambda: "d")
            svc, index, adapters = self._service(tmp, rate_limiter=rl)
            first = await svc.handle_command(UserCommand(command_type="new_topic", text="a"), sender_id="u")
            self.assertTrue(first["ok"])
            second = await svc.handle_command(UserCommand(command_type="new_topic", text="b"), sender_id="u")
            self.assertEqual(second["reason"], "rate_limited")
            # status is not rate-limited
            status = await svc.handle_command(UserCommand(command_type="status"), sender_id="u")
            self.assertTrue(status["ok"])

    async def test_new_topic_routes_to_planning(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc, _, adapters = self._service(tmp)
            res = await svc.handle_command(parse_user_command("做一个关于AI老师的短片"))
            self.assertTrue(res["ok"])
            self.assertEqual(adapters.calls[0][0], "plan")
            self.assertIn("AI老师", adapters.calls[0][1]["idea"])

    async def test_regenerate_maps_to_zero_based(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc, _, adapters = self._service(tmp)
            res = await svc.handle_command(parse_user_command("重生成第 4 镜"))
            self.assertTrue(res["ok"])
            self.assertEqual(adapters.calls[0], ("regen", {"shot_idx": 3}))

    async def test_approve_resolves_pending_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc, index, _ = self._service(tmp)
            record = index.create(idea="x")
            index.create_review_task(record["session_id"], stage="storyboard", summary="共 8 镜")
            res = await svc.handle_command(parse_user_command("通过"))
            self.assertTrue(res["ok"])
            self.assertEqual(res["stage"], "storyboard")
            tasks = index.list_review_tasks(record["session_id"])
            self.assertEqual(tasks[0]["status"], "approved")

    async def test_approve_final_completes_locally(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc, index, adapters = self._service(tmp)
            record = index.create(idea="x")
            index.create_review_task(record["session_id"], stage="final", summary="成片")
            res = await svc.handle_command(UserCommand(command_type="approve"))
            self.assertTrue(res["ok"])
            self.assertTrue(res["local_completed"])
            self.assertEqual(index.get(record["session_id"])["stage"], "completed")
            self.assertFalse(any(call[0] == "publish" for call in adapters.calls))

    async def test_publish_command_explicitly_invokes_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc, index, adapters = self._service(tmp)
            record = index.create(idea="x")
            res = await svc.handle_command(UserCommand(command_type="publish"), sender_id="u1")
            self.assertTrue(res["ok"])
            self.assertIn(("publish", {"session_id": record["session_id"], "target": "u1"}), adapters.calls)

    async def test_revise_records_instruction(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc, index, _ = self._service(tmp)
            record = index.create(idea="x")
            index.create_review_task(record["session_id"], stage="script", summary="s")
            res = await svc.handle_command(parse_user_command("修改：结尾更温暖"))
            self.assertTrue(res["ok"])
            self.assertEqual(res["instruction"], "结尾更温暖")
            self.assertEqual(index.list_review_tasks(record["session_id"])[0]["status"], "revised")

    async def test_lifecycle_updates_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc, index, _ = self._service(tmp)
            record = index.create(idea="x")
            res = await svc.handle_command(UserCommand(command_type="pause"))
            self.assertTrue(res["ok"])
            self.assertEqual(index.get(record["session_id"])["stage"], "paused")


class FakeEngine(WorkflowEngine):
    """WorkflowEngine with generation stubbed out so the state machine is tested
    without calling any LLM/image/video model."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = []

    async def _gen_script(self, session, instruction=""):
        self.calls.append(("script", instruction))
        return "剧本 summary"

    async def _gen_storyboard(self, session, instruction=""):
        self.calls.append(("storyboard", instruction))
        return "分镜 summary"

    async def _gen_video(self, session, instruction="", progress=None):
        self.calls.append(("video", instruction))
        if progress:
            progress("video_clip_start", "rendering")
        return "视频 summary"

    async def _do_publish(self, session):
        self.calls.append(("publish", ""))
        return "published url"


class TestWorkflowEngineStateMachine(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _write_storyboard(index, sid, shot_count=1):
        scene_dir = index.working_dir(sid) / "idea2video" / "scene_0"
        scene_dir.mkdir(parents=True, exist_ok=True)
        (scene_dir / "storyboard.json").write_text(
            json.dumps([{"idx": idx} for idx in range(shot_count)]),
            encoding="utf-8",
        )
        return scene_dir

    async def test_final_review_rolls_back_when_shot_artifacts_are_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            engine = FakeEngine(index, tmp)
            sid = index.create(idea="topic")["session_id"]
            self._write_storyboard(index, sid, shot_count=2)
            final_review = index.create_review_task(sid, "final", "review")
            index.update_stage(sid, "final_review_pending", "review")

            result = await engine.approve(sid)

            self.assertFalse(result["ok"])
            self.assertTrue(result["rolled_back"])
            self.assertEqual(result["stage"], "shot_video")
            self.assertEqual(engine.calls, [])
            self.assertEqual(index.get(sid)["stage"], "shot_video_review_pending")
            tasks = index.list_review_tasks(sid)
            self.assertEqual(
                next(task for task in tasks if task["review_id"] == final_review["review_id"])["status"],
                "superseded",
            )
            self.assertEqual(
                [task["stage"] for task in tasks if task["status"] == "pending"],
                ["shot_video"],
            )

    async def test_incomplete_shot_review_recovers_without_advancing(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            engine = FakeEngine(index, tmp)
            sid = index.create(idea="topic")["session_id"]
            scene_dir = self._write_storyboard(index, sid)
            (scene_dir / "quality.json").write_text("{}", encoding="utf-8")
            (scene_dir / "final_video_with_subtitles.mp4").write_bytes(b"stale")
            index.create_review_task(sid, "shot_video", "review")
            index.update_stage(sid, "shot_video_review_pending", "review")

            result = await engine.approve(sid)

            self.assertFalse(result["ok"])
            self.assertEqual(result["stage"], "shot_video")
            self.assertEqual([call[0] for call in engine.calls], ["video"])
            self.assertEqual(index.get(sid)["stage"], "shot_video_review_pending")
            archive = scene_dir.parent / "_archive" / "incomplete_recovery" / "v1" / "scene_0"
            self.assertTrue((archive / "quality.json").is_file())
            self.assertTrue((archive / "final_video_with_subtitles.mp4").is_file())

    async def test_shot_regeneration_rebuilds_and_archives_project_final(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            engine = FakeEngine(index, tmp)
            sid = index.create(idea="topic")["session_id"]
            index.create_review_task(sid, "shot_video", "review")
            index.update_stage(sid, "shot_video_review_pending", "review")
            final_path = Path(tmp) / ".working_dir" / sid / "idea2video" / "final_video.mp4"
            final_path.parent.mkdir(parents=True, exist_ok=True)
            final_path.write_bytes(b"old")

            async def rebuild_video(session, instruction="", progress=None):
                engine.calls.append(("video", instruction))
                final_path.write_bytes(b"new")
                return "rebuilt"

            engine._gen_video = rebuild_video
            result = await engine.rebuild_after_shot_regeneration(sid)

            archived = Path(tmp) / result["archived_final_video_path"]
            self.assertEqual(archived.read_bytes(), b"old")
            self.assertEqual(final_path.read_bytes(), b"new")
            self.assertEqual(index.get(sid)["stage"], "shot_video_review_pending")
            self.assertEqual(engine.calls, [("video", "")])

    async def test_shot_regeneration_from_final_review_reopens_video_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            engine = FakeEngine(index, tmp)
            sid = index.create(idea="topic")["session_id"]
            final_review = index.create_review_task(sid, "final", "review")
            index.update_stage(sid, "final_review_pending", "review")

            await engine.rebuild_after_shot_regeneration(sid)

            tasks = index.list_review_tasks(sid)
            previous = next(
                task for task in tasks if task["review_id"] == final_review["review_id"]
            )
            self.assertEqual(previous["status"], "superseded")
            self.assertEqual(
                [task["stage"] for task in tasks if task["status"] == "pending"],
                ["shot_video"],
            )
            self.assertEqual(index.get(sid)["stage"], "shot_video_review_pending")

    async def test_keyframe_preview_keeps_storyboard_review_open(self):
        class PreviewHandler:
            stage = "shot_video"

            async def run(self, engine, session, instruction="", progress=None):
                return "video"

            async def preview_keyframes(self, engine, session, progress=None):
                return "关键帧预览已生成，共 2 个镜头。"

        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            handlers = StageHandlerRegistry.default()
            handlers.register(PreviewHandler(), replace=True)
            engine = WorkflowEngine(index, tmp, stage_handlers=handlers)
            record = index.create(idea="topic")
            sid = record["session_id"]
            index.create_review_task(sid, "storyboard", "review")
            index.update_stage(sid, "storyboard_review_pending", "review")

            result = await engine.preview_keyframes(sid)

            self.assertTrue(result["ok"])
            self.assertEqual(index.get(sid)["stage"], "storyboard_review_pending")
            self.assertTrue(index.get(sid)["keyframe_preview"]["ready"])
            pending = [task for task in index.list_review_tasks(sid) if task["status"] == "pending"]
            self.assertEqual([task["stage"] for task in pending], ["storyboard"])

    async def test_keyframe_preview_rejects_other_stages(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            engine = WorkflowEngine(index, tmp)
            sid = index.create(idea="topic")["session_id"]
            result = await engine.preview_keyframes(sid)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "invalid_stage")

    async def test_single_keyframe_preview_targets_one_shot_and_keeps_review_open(self):
        calls = []

        class PreviewHandler:
            stage = "shot_video"

            async def run(self, engine, session, instruction="", progress=None):
                return "video"

            async def preview_keyframes(self, engine, session, progress=None, **kwargs):
                calls.append(kwargs)
                return "单镜首帧已生成"

        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            handlers = StageHandlerRegistry.default()
            handlers.register(PreviewHandler(), replace=True)
            engine = WorkflowEngine(index, tmp, stage_handlers=handlers)
            record = index.create(idea="topic")
            sid = record["session_id"]
            scene_dir = index.working_dir(sid) / "idea2video" / "scene_0"
            scene_dir.mkdir(parents=True)
            (scene_dir / "storyboard.json").write_text(
                json.dumps([{"idx": 0, "visual_desc": "雨夜办公室"}], ensure_ascii=False),
                encoding="utf-8",
            )
            index.create_review_task(sid, "storyboard", "review")
            index.update_stage(sid, "storyboard_review_pending", "review")

            result = await engine.preview_keyframes(
                sid, scene_index=0, shot_index=0, force=True)

            self.assertTrue(result["ok"])
            self.assertEqual(calls, [{"scene_index": 0, "shot_index": 0, "force": True}])
            self.assertEqual(index.get(sid)["stage"], "storyboard_review_pending")
            self.assertEqual(index.get(sid)["keyframe_preview"]["completed_shots"], ["0_0"])

    async def test_full_gated_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            engine = FakeEngine(index, tmp)

            r = await engine.start_topic("AI 老师进山村")
            sid = r["session_id"]
            self.assertEqual(r["stage"], "script")
            self.assertEqual(index.get(sid)["stage"], "script_review_pending")

            self.assertEqual((await engine.approve(sid))["stage"], "storyboard")
            self.assertEqual(index.get(sid)["stage"], "storyboard_review_pending")
            self.assertEqual((await engine.approve(sid))["stage"], "shot_video")
            self.assertEqual((await engine.approve(sid))["stage"], "final")
            done = await engine.approve(sid)
            self.assertEqual(done["stage"], "completed")
            self.assertEqual(index.get(sid)["stage"], "completed")

            # Finishing production is local-only; sharing is a separate explicit action.
            self.assertEqual([c[0] for c in engine.calls], ["script", "storyboard", "video"])
            # four review tasks, all approved; the last gate completes locally.
            tasks = index.list_review_tasks(sid)
            self.assertEqual([t["stage"] for t in tasks], ["script", "storyboard", "shot_video", "final"])
            self.assertTrue(all(t["status"] == "approved" for t in tasks))

    def test_interrupted_gate_helper(self):
        self.assertEqual(WorkflowEngine._interrupted_gate("shot_video_generating"), "shot_video")
        self.assertEqual(WorkflowEngine._interrupted_gate("storyboard_revision_requested"), "storyboard")
        self.assertEqual(WorkflowEngine._interrupted_gate("script_generating"), "script")
        self.assertIsNone(WorkflowEngine._interrupted_gate("storyboard_review_pending"))
        self.assertIsNone(WorkflowEngine._interrupted_gate("completed"))

    async def test_resume_after_interruption_recovers_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            engine = FakeEngine(index, tmp)
            sid = (await engine.start_topic("topic"))["session_id"]
            await engine.approve(sid)  # -> storyboard_review_pending
            # simulate a crash mid 分镜视频 generation: stage stuck *_generating, no job
            index.update_stage(sid, "shot_video_generating", "Generating shot_video")
            engine.calls.clear()
            res = await engine.resume_generation(sid)
            self.assertTrue(res["ok"]) ; self.assertTrue(res.get("resumed"))
            self.assertEqual(res["stage"], "shot_video")
            self.assertEqual(index.get(sid)["stage"], "shot_video_review_pending")
            self.assertEqual([c[0] for c in engine.calls], ["video"])  # re-ran the interrupted gate
            # the now-open review task is shot_video and pending
            pend = [t for t in index.list_review_tasks(sid) if t["status"] == "pending"]
            self.assertEqual([t["stage"] for t in pend], ["shot_video"])

    async def test_resume_noop_when_not_interrupted(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            engine = FakeEngine(index, tmp)
            sid = (await engine.start_topic("topic"))["session_id"]   # script_review_pending
            engine.calls.clear()
            res = await engine.resume_generation(sid)
            self.assertTrue(res["ok"]); self.assertIn("无中断", res.get("note", ""))
            self.assertEqual(engine.calls, [])  # nothing re-run

    def test_per_session_language_and_aspect_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = FakeEngine(SessionIndex(tmp), tmp)
            # English video: session overrides win over the (empty here) global config
            eff = engine._effective_config({"target_language": "en", "aspect_ratio": "portrait"})
            self.assertEqual(eff["language"]["target_language"], "en")
            self.assertEqual(eff["video"]["aspect_ratio"], "portrait")
            self.assertIn("English mode", engine._lang_instruction({"target_language": "en"}))
            self.assertIn("简体中文", engine._lang_instruction({"target_language": "zh-CN"}))  # zh -> 中文约束
            # no per-session value -> falls back to global default
            self.assertEqual(engine._lang_instruction({}), engine._chinese_instruction())
            # per-video audio/subtitle overrides flow into the effective config
            eff2 = engine._effective_config({"overrides": {"subtitle_enabled": False, "subtitle_burn_in": False, "tts_enabled": True, "voice": "nova"}})
            self.assertIs(eff2["subtitle"]["enabled"], False)
            self.assertIs(eff2["subtitle"]["burn_in"], False)
            self.assertIs(eff2["audio"]["tts"]["enabled"], True)
            self.assertEqual(eff2["audio"]["tts"]["voice"], "nova")
            eff_packaging = engine._effective_config({"overrides": {"hook_enabled": True, "cover_enabled": True}})
            self.assertTrue(eff_packaging["video"]["hook"]["enabled"])
            self.assertTrue(eff_packaging["video"]["cover"]["enabled"])
            minimax = engine._effective_config({
                "config_snapshot": {"audio": {"tts": {"provider": "minimax", "voice_id": "old"}}},
                "overrides": {"voice": "new"},
            })
            self.assertEqual(minimax["audio"]["tts"]["voice_id"], "new")
            # per-video BGM: a real library track -> enabled + resolved path; "__none__" -> off
            import os
            bgm_dir = os.path.join(tmp, "assets", "bgm"); os.makedirs(bgm_dir, exist_ok=True)
            open(os.path.join(bgm_dir, "epic.mp3"), "wb").write(b"MP3")
            eff3 = engine._effective_config({"overrides": {"bgm_track": "epic.mp3"}})
            self.assertIs(eff3["audio"]["bgm"]["enabled"], True)
            self.assertTrue(eff3["audio"]["bgm"]["path"].endswith("epic.mp3"))
            eff4 = engine._effective_config({"overrides": {"bgm_track": "__none__"}})
            self.assertIs(eff4["audio"]["bgm"]["enabled"], False)

    async def test_start_topic_stores_per_video_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp); engine = FakeEngine(index, tmp)
            sid = (await engine.start_topic("topic", target_language="en", aspect_ratio="portrait"))["session_id"]
            rec = index.get(sid)
            self.assertEqual(rec["target_language"], "en")
            self.assertEqual(rec["aspect_ratio"], "portrait")

    async def test_project_config_snapshot_is_stable_and_excludes_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "configs"
            config_dir.mkdir()
            config_path = config_dir / "idea2video.yaml"
            config_path.write_text(
                "subtitle:\n  enabled: true\n  burn_in: true\n"
                "audio:\n  tts:\n    enabled: false\n    api_key: secret\n",
                encoding="utf-8",
            )
            index = SessionIndex(tmp)
            engine = FakeEngine(index, tmp)
            sid = (await engine.start_topic("topic"))["session_id"]
            record = index.get(sid)
            self.assertTrue(record["config_snapshot"]["subtitle"]["burn_in"])
            self.assertNotIn("api_key", record["config_snapshot"]["audio"]["tts"])
            config_path.write_text("subtitle:\n  enabled: false\n  burn_in: false\n", encoding="utf-8")
            self.assertTrue(engine._effective_config(index.get(sid))["subtitle"]["burn_in"])

    async def test_moderation_settings_reload_without_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "configs"
            config_dir.mkdir()
            config_path = config_dir / "idea2video.yaml"
            config_path.write_text("moderation:\n  enabled: false\n", encoding="utf-8")
            engine = FakeEngine(SessionIndex(tmp), tmp)
            allowed = await engine.start_topic("blocked phrase")
            self.assertTrue(allowed["ok"])
            config_path.write_text(
                "moderation:\n  enabled: true\n  keywords: [blocked]\n",
                encoding="utf-8",
            )
            rejected = await engine.start_topic("blocked phrase")
            self.assertFalse(rejected["ok"])
            self.assertEqual(rejected["error"], "moderation")

    def test_reopen_rejects_bad_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = FakeEngine(SessionIndex(tmp), tmp)
            self.assertFalse(engine.reopen("any", "shot_video")["ok"])

    async def test_reopen_storyboard_invalidates_downstream_keeps_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            engine = FakeEngine(index, tmp)
            sid = (await engine.start_topic("topic"))["session_id"]
            idea = engine._idea_dir(sid)
            scene = idea / "scene_0"; (scene / "shots" / "0").mkdir(parents=True)
            (scene / "shots" / "0" / "video.mp4").write_text("v", encoding="utf-8")
            (scene / "storyboard.json").write_text("[]", encoding="utf-8")
            (scene / "camera_tree.json").write_text("[]", encoding="utf-8")
            (idea / "final_video.mp4").write_text("f", encoding="utf-8")

            r = engine.reopen(sid, "storyboard")
            self.assertTrue(r["ok"]); self.assertEqual(r["stage"], "storyboard")
            self.assertFalse((scene / "shots").exists())          # shots dropped
            self.assertFalse((idea / "final_video.mp4").exists())  # final dropped
            self.assertFalse((scene / "camera_tree.json").exists())
            self.assertTrue((scene / "storyboard.json").exists())  # gate artifact kept
            self.assertEqual(index.get(sid)["stage"], "storyboard_review_pending")
            pend = [t for t in index.list_review_tasks(sid) if t["status"] == "pending"]
            self.assertEqual([t["stage"] for t in pend], ["storyboard"])

    async def test_reopen_script_also_drops_storyboard(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            engine = FakeEngine(index, tmp)
            sid = (await engine.start_topic("t"))["session_id"]
            idea = engine._idea_dir(sid); scene = idea / "scene_0"; scene.mkdir(parents=True)
            (scene / "storyboard.json").write_text("[]", encoding="utf-8")
            r = engine.reopen(sid, "script")
            self.assertTrue(r["ok"])
            self.assertFalse((scene / "storyboard.json").exists())  # storyboard also dropped
            self.assertEqual(index.get(sid)["stage"], "script_review_pending")

    async def test_stage_failure_does_not_strand_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)

            class FlakyEngine(FakeEngine):
                fail = True

                async def _gen_storyboard(self, session, instruction=""):
                    if FlakyEngine.fail:
                        raise RuntimeError("image model 503")
                    return await super()._gen_storyboard(session, instruction)

            engine = FlakyEngine(index, tmp)
            r = await engine.start_topic("topic")
            sid = r["session_id"]

            # approve script -> storyboard gen fails
            failed = await engine.approve(sid)
            self.assertFalse(failed["ok"])
            self.assertEqual(failed["failed_stage"], "storyboard")
            # script review stays pending (re-approvable), not consumed
            tasks = index.list_review_tasks(sid)
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0]["status"], "pending")
            self.assertEqual(index.get(sid)["stage"], "script_review_pending")

            # fix the issue and retry the same 通过 -> advances
            FlakyEngine.fail = False
            ok = await engine.approve(sid)
            self.assertTrue(ok["ok"])
            self.assertEqual(ok["stage"], "storyboard")
            self.assertEqual([t["status"] for t in index.list_review_tasks(sid)], ["approved", "pending"])

    async def test_revise_reruns_current_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            engine = FakeEngine(index, tmp)
            r = await engine.start_topic("topic")
            sid = r["session_id"]

            rev = await engine.revise(sid, "结尾更温暖")
            self.assertEqual(rev["stage"], "script")
            self.assertTrue(rev["revised"])
            # script generated twice (initial + revision), instruction passed through
            self.assertEqual(engine.calls, [("script", ""), ("script", "结尾更温暖")])
            # old review revised, new review pending
            tasks = index.list_review_tasks(sid)
            self.assertEqual(tasks[0]["status"], "revised")
            self.assertEqual(tasks[1]["status"], "pending")

    async def test_trigger_service_drives_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            engine = FakeEngine(index, tmp)
            svc = TriggerService(index, FakeAdapters(), workflow_engine=engine)

            started = await svc.handle_command(parse_user_command("做一个山村支教的短片"))
            self.assertEqual(started["stage"], "script")
            approved = await svc.handle_command(parse_user_command("通过"))
            self.assertEqual(approved["stage"], "storyboard")
            revised = await svc.handle_command(parse_user_command("修改：节奏快一点"))
            self.assertEqual(revised["stage"], "storyboard")
            self.assertTrue(revised["revised"])


class TestJobRunner(unittest.TestCase):
    def _wait(self, runner, job_id, timeout=5.0):
        for _ in range(int(timeout / 0.02)):
            j = runner.get(job_id)
            if j and j["state"] != "running":
                return j
            time.sleep(0.02)
        raise AssertionError("job did not finish")

    def test_runs_and_returns_result(self):
        runner = JobRunner()

        async def w():
            return {"x": 1}

        rec = runner.submit(w)
        self.assertTrue(rec["accepted"])
        self.assertEqual(self._wait(runner, rec["job_id"])["result"], {"x": 1})

    def test_failure_is_captured(self):
        runner = JobRunner()

        async def w():
            raise RuntimeError("boom")

        rec = runner.submit(w)
        job = self._wait(runner, rec["job_id"])
        self.assertEqual(job["state"], "failed")
        self.assertIn("boom", job["error"])

    def test_single_flight_per_key(self):
        runner = JobRunner()
        gate = threading.Event()

        async def slow():
            gate.wait()
            return "ok"

        r1 = runner.submit(slow, key="s1")
        self.assertTrue(r1["accepted"])
        r2 = runner.submit(slow, key="s1")
        self.assertFalse(r2["accepted"])  # busy: same key still running
        self.assertEqual(r2["state"], "busy")
        gate.set()
        self._wait(runner, r1["job_id"])

        async def quick():
            return "done"

        self.assertTrue(runner.submit(quick, key="s1")["accepted"])  # key freed


class TestBudgetGuard(unittest.TestCase):
    def test_check_render_limits(self):
        bg = BudgetGuard(max_scenes=2, max_total_shots=10)
        self.assertTrue(bg.check_render(1, 5)[0])
        self.assertFalse(bg.check_render(3, 5)[0])   # scenes over
        self.assertFalse(bg.check_render(1, 20)[0])  # shots over
        self.assertTrue(BudgetGuard().check_render(99, 999)[0])  # no limits

    def test_from_config(self):
        bg = BudgetGuard.from_config({
            "generation_budget": {"max_scenes": 3, "max_total_shots": 12},
            "rate_limits": {"global": {"max_concurrent_generations": 2}},
        })
        self.assertEqual(bg.max_total_shots, 12)
        self.assertEqual(bg.max_concurrent_generations, 2)


class TestJobRunnerConcurrencyCap(unittest.TestCase):
    def test_global_cap(self):
        runner = JobRunner(max_concurrent=1)
        gate = threading.Event()

        async def slow():
            gate.wait()
            return 1

        a = runner.submit(slow, key="a")
        self.assertTrue(a["accepted"])
        b = runner.submit(slow, key="b")  # different key, but global cap=1
        self.assertFalse(b["accepted"])
        self.assertEqual(b["state"], "at_capacity")
        gate.set()
        for _ in range(250):
            if runner.get(a["job_id"])["state"] != "running":
                break
            time.sleep(0.02)

        async def quick():
            return 2

        self.assertTrue(runner.submit(quick, key="c")["accepted"])  # capacity freed


class TestEngineBudgetGate(unittest.IsolatedAsyncioTestCase):
    async def test_video_stage_blocked_when_too_many_shots(self):
        import json
        import os
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            engine = FakeEngine(index, tmp, budget=BudgetGuard(max_total_shots=3))
            sid = (await engine.start_topic("topic"))["session_id"]
            await engine.approve(sid)  # script -> storyboard (stub writes nothing)

            scene0 = os.path.join(str(index.working_dir(sid)), "idea2video", "scene_0")
            os.makedirs(scene0, exist_ok=True)
            json.dump([{} for _ in range(5)], open(os.path.join(scene0, "storyboard.json"), "w", encoding="utf-8"))

            res = await engine.approve(sid)  # storyboard -> video gate -> budget check
            self.assertFalse(res["ok"])
            self.assertEqual(res["error"], "budget_exceeded")
            # storyboard review stays pending so the user can reduce shots and retry
            tasks = index.list_review_tasks(sid)
            self.assertTrue(any(t["stage"] == "storyboard" and t["status"] == "pending" for t in tasks))


class TestManualStoryboardEdit(unittest.TestCase):
    def _engine(self, tmp):
        index = SessionIndex(tmp)
        return index, WorkflowEngine(index, tmp)

    def _seed(self, index, sid):
        import json, os
        scene0 = os.path.join(str(index.working_dir(sid)), "idea2video", "scene_0")
        shots = os.path.join(scene0, "shots", "0"); os.makedirs(shots, exist_ok=True)
        json.dump([{"idx": 0, "is_last": True, "cam_idx": 0, "visual_desc": "old", "audio_desc": ""}],
                  open(os.path.join(scene0, "storyboard.json"), "w", encoding="utf-8"))
        open(os.path.join(scene0, "camera_tree.json"), "w").write("[]")
        open(os.path.join(shots, "shot_description.json"), "w").write("{}")
        return scene0

    def test_edit_adds_renumbers_and_invalidates(self):
        import json, os
        with tempfile.TemporaryDirectory() as tmp:
            index, eng = self._engine(tmp)
            sid = index.create(idea="t", mode="idea")["session_id"]
            index.update_stage(sid, "storyboard_review_pending", "x")
            scene0 = self._seed(index, sid)
            res = eng.edit_storyboard(sid, [{"scene_index": 0, "shots": [
                {"visual_desc": "A wide shot", "audio_desc": "[Narrator]: 旁白", "screen_text": "邮件", "screen_text_pos": "top",
                 "duration_sec": 6, "director_desc": "0-3秒，人物保持目光。",
                 "beats": [{"start_sec": 0, "end_sec": 3, "action": "He holds eye contact.", "performance": "Controlled breath.", "camera": "Static"}],
                 "visual_style": ["cinematic"], "avoid": ["fast cutting"]},
                {"visual_desc": "A close up", "audio_desc": ""},
            ]}])
            self.assertTrue(res["ok"]); self.assertEqual(res["shots"], 2)
            sb = json.load(open(os.path.join(scene0, "storyboard.json"), encoding="utf-8"))
            self.assertEqual([(s["idx"], s["is_last"], s["cam_idx"]) for s in sb], [(0, False, 0), (1, True, 1)])
            self.assertEqual(sb[0]["screen_text"], "邮件")
            self.assertEqual(sb[0]["duration_sec"], 6.0)
            self.assertEqual(sb[0]["director_desc"], "0-3秒，人物保持目光。")
            self.assertEqual(sb[0]["beats"][0]["performance"], "Controlled breath.")
            self.assertEqual(sb[0]["visual_style"], ["cinematic"])
            self.assertEqual(sb[0]["avoid"], ["fast cutting"])
            self.assertFalse(os.path.exists(os.path.join(scene0, "camera_tree.json")))  # derived cache dropped
            self.assertFalse(os.path.exists(os.path.join(scene0, "shots")))

    def test_rejects_missing_visual_and_empty_scene(self):
        with tempfile.TemporaryDirectory() as tmp:
            index, eng = self._engine(tmp)
            sid = index.create(idea="t", mode="idea")["session_id"]
            index.update_stage(sid, "storyboard_review_pending", "x")
            self._seed(index, sid)
            self.assertEqual(eng.edit_storyboard(sid, [{"scene_index": 0, "shots": [{"visual_desc": ""}]}])["error"],
                             "missing_visual")
            self.assertEqual(eng.edit_storyboard(sid, [{"scene_index": 0, "shots": []}])["error"], "empty_scene")

    def test_chinese_project_rejects_new_english_but_allows_unchanged_legacy_fields(self):
        import json, os
        with tempfile.TemporaryDirectory() as tmp:
            index, eng = self._engine(tmp)
            sid = index.create(idea="中文短剧", target_language="zh-CN")["session_id"]
            index.update_stage(sid, "storyboard_review_pending", "x")
            scene0 = self._seed(index, sid)
            legacy = [{
                "visual_desc": "An old wide shot.",
                "director_desc": "固定广角镜头，人物从左侧进入。",
                "audio_desc": "",
                "duration_sec": 6,
            }]
            with open(os.path.join(scene0, "storyboard.json"), "w", encoding="utf-8") as handle:
                json.dump([{
                    "idx": 0, "is_last": True, "cam_idx": 0, **legacy[0],
                }], handle, ensure_ascii=False)

            legacy[0]["duration_sec"] = 7
            self.assertTrue(eng.edit_storyboard(sid, [{"scene_index": 0, "shots": legacy}])["ok"])

            legacy[0]["visual_desc"] = "A newly written English close-up."
            rejected = eng.edit_storyboard(sid, [{"scene_index": 0, "shots": legacy}])
            self.assertEqual(rejected["error"], "non_chinese_storyboard")

            legacy[0]["visual_desc"] = "固定广角镜头，人物从画面左侧进入办公室。"
            self.assertTrue(eng.edit_storyboard(sid, [{"scene_index": 0, "shots": legacy}])["ok"])

    def test_rejected_outside_storyboard_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            index, eng = self._engine(tmp)
            sid = index.create(idea="t", mode="idea")["session_id"]
            index.update_stage(sid, "shot_video_review_pending", "x")
            self._seed(index, sid)
            self.assertEqual(eng.edit_storyboard(sid, [{"scene_index": 0, "shots": [{"visual_desc": "x"}]}])["error"],
                             "wrong_stage")


class TestManualScriptEdit(unittest.TestCase):
    def test_edit_updates_readable_and_pipeline_scripts_and_invalidates(self):
        import json, os
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            eng = WorkflowEngine(index, tmp)
            sid = index.create(idea="t", mode="script", script="old")["session_id"]
            index.update_stage(sid, "script_review_pending", "x")
            idea = os.path.join(str(index.working_dir(sid)), "idea2video")
            os.makedirs(os.path.join(idea, "scene_0", "shots", "0"), exist_ok=True)
            open(os.path.join(idea, "scene_0", "storyboard.json"), "w", encoding="utf-8").write("[]")
            open(os.path.join(idea, "final_video.mp4"), "wb").write(b"old")

            text = "场景一：办公室\n王云宝：我会回来。\n\n场景二：天台\n他抬头看向晨光。"
            result = eng.edit_script(sid, text)

            self.assertTrue(result["ok"])
            self.assertEqual(result["scenes"], 2)
            self.assertEqual(open(os.path.join(idea, "story.txt"), encoding="utf-8").read(), text)
            self.assertEqual(len(json.load(open(os.path.join(idea, "script.json"), encoding="utf-8"))), 2)
            self.assertFalse(os.path.exists(os.path.join(idea, "scene_0", "storyboard.json")))
            self.assertFalse(os.path.exists(os.path.join(idea, "final_video.mp4")))
            self.assertEqual(index.get(sid)["script"], text)

    def test_rejects_empty_or_wrong_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            eng = WorkflowEngine(index, tmp)
            sid = index.create(idea="t")["session_id"]
            index.update_stage(sid, "script_review_pending", "x")
            self.assertEqual(eng.edit_script(sid, "  ")["error"], "empty")
            index.update_stage(sid, "storyboard_review_pending", "x")
            self.assertEqual(eng.edit_script(sid, "场景一：办公室")["error"], "wrong_stage")


class TestProductionServiceNotify(unittest.IsolatedAsyncioTestCase):
    async def _wait_push(self, pushes, timeout=5.0):
        waited = 0.0
        while waited < timeout:
            if pushes:
                return
            await asyncio.sleep(0.02)
            waited += 0.02
        raise AssertionError("no push arrived")

    async def test_start_topic_runs_in_background_and_pushes(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            engine = FakeEngine(index, tmp)
            pushes = []

            async def notifier(text, target):
                pushes.append((target, text))

            svc = ProductionService(engine, JobRunner(), notifier=notifier)
            rec = svc.start_topic("王云宝", target="user1")
            self.assertTrue(rec["accepted"])  # returns immediately
            await self._wait_push(pushes)
            target, text = pushes[0]
            self.assertEqual(target, "user1")
            self.assertIn("script", text)

    async def test_web_call_without_target_does_not_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            pushes = []

            async def notifier(text, target):
                pushes.append(1)

            svc = ProductionService(FakeEngine(index, tmp), JobRunner(), notifier=notifier)
            rec = svc.start_topic("x")  # target=None (web polls instead)
            for _ in range(200):
                j = svc.job(rec["job_id"])
                if j and j["state"] != "running":
                    break
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.05)
            self.assertEqual(pushes, [])


class TestTriggerBackground(unittest.IsolatedAsyncioTestCase):
    async def test_new_topic_acks_now_and_pushes_when_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = SessionIndex(tmp)
            pushes = []

            async def notifier(text, target):
                pushes.append((target, text))

            svc = ProductionService(FakeEngine(index, tmp), JobRunner(), notifier=notifier)
            trig = TriggerService(index, FakeAdapters(), production_service=svc)

            res = await trig.handle_command(parse_user_command("做一个短片"), sender_id="u1", channel="console")
            self.assertTrue(res["accepted"])  # immediate ack
            self.assertIn("job_id", res)

            waited = 0.0
            while waited < 5.0 and not pushes:
                await asyncio.sleep(0.02)
                waited += 0.02
            self.assertTrue(any(t == "u1" for t, _ in pushes))


if __name__ == "__main__":
    unittest.main()
