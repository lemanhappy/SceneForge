"""Tests for config API, production API, and the unified app router."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import yaml

from agent_runtime.models import ToolResult
from agent_runtime.session_index import SessionIndex
from server import AppAPI, ConfigAPI, ConfigService, ProductionAPI
from services import WorkflowEngine


def run(coro):
    return asyncio.run(coro)


class TestConfigService(unittest.TestCase):
    def test_get_masks_keys_and_update_roundtrip(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "agent.local.yaml")
            svc = ConfigService(path)

            view = svc.get()
            self.assertIn("llm", view)
            self.assertEqual(view["llm"]["api_key"], {"set": False, "hint": ""})

            svc.update("llm", {"model": "gemini-2.5-flash", "api_key": "sk-secret-1234"})
            view = svc.get()
            self.assertEqual(view["llm"]["model"], "gemini-2.5-flash")
            self.assertTrue(view["llm"]["api_key"]["set"])
            self.assertEqual(view["llm"]["api_key"]["hint"], "…1234")  # masked, last 4

            # raw file has the real key; empty api_key update does not wipe it
            svc.update("llm", {"model": "gemini-2.5-pro", "api_key": ""})
            raw = yaml.safe_load(open(path, encoding="utf-8"))
            self.assertEqual(raw["llm"]["api_key"], "sk-secret-1234")
            self.assertEqual(raw["llm"]["model"], "gemini-2.5-pro")

    def test_video_provider_derived_and_unknown_section(self):
        with tempfile.TemporaryDirectory() as root:
            svc = ConfigService(os.path.join(root, "a.yaml"))
            svc.update("video", {"base_url": "https://yunwu.ai", "model": "doubao-seedance-1-5-pro"})
            self.assertEqual(svc.get()["video"]["provider_derived"], "yunwu")
            with self.assertRaises(ValueError):
                svc.update("bogus", {"x": 1})

    def test_video_profile_crud_masks_keys_and_preserves_legacy(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "agent.local.yaml")
            with open(path, "w", encoding="utf-8") as handle:
                yaml.safe_dump({"video": {
                    "model": "legacy-seedance",
                    "base_url": "https://yunwu.ai/v1",
                    "api_key": "shared-secret",
                }}, handle)
            svc = ConfigService(path)

            initial = svc.get_video_profiles()
            self.assertTrue(initial["synthetic_legacy"])
            self.assertEqual(initial["default_profile_id"], "legacy")

            result = svc.upsert_video_profile("cinema", {
                "label": "电影质量",
                "enabled": True,
                "provider": "veo",
                "model": "veo-quality",
                "base_url": "https://yunwu.ai/v1",
                "quality_tier": "quality",
                "estimated_cost": 4.2,
                "supported_durations": [8],
                "max_reference_count": 2,
            })
            cinema = next(item for item in result["profiles"] if item["profile_id"] == "cinema")
            self.assertEqual(cinema["api_key"], {"set": True, "hint": "…cret"})
            self.assertTrue(cinema["api_key_inherited"])
            self.assertNotIn("shared-secret", str(result))

            active = svc.activate_video_profile("cinema")
            self.assertEqual(active["default_profile_id"], "cinema")
            after_delete = svc.delete_video_profile("legacy")
            self.assertEqual([item["profile_id"] for item in after_delete["profiles"]], ["cinema"])

            raw = yaml.safe_load(open(path, encoding="utf-8"))
            self.assertEqual(raw["video"]["api_key"], "shared-secret")
            self.assertEqual(raw["video_profiles"]["default"], "cinema")


class TestConfigAPI(unittest.TestCase):
    def test_routes(self):
        with tempfile.TemporaryDirectory() as root:
            api = ConfigAPI(ConfigService(os.path.join(root, "a.yaml")))
            self.assertEqual(run(api.handle("GET", "/api/config"))[0], 200)
            status, body = run(api.handle("PUT", "/api/config/image", {"model": "gemini-2.5-flash-image"}))
            self.assertEqual(status, 200)
            self.assertEqual(body["config"]["model"], "gemini-2.5-flash-image")
            self.assertEqual(run(api.handle("PUT", "/api/config/bogus", {}))[0], 400)

    def test_video_profile_routes(self):
        with tempfile.TemporaryDirectory() as root:
            api = ConfigAPI(ConfigService(os.path.join(root, "a.yaml")))
            status, result = run(api.handle("POST", "/api/config/video-profiles", {
                "profile_id": "fast",
                "label": "快速",
                "model": "seedance-fast",
                "base_url": "https://yunwu.ai/v1",
            }))
            self.assertEqual(status, 200)
            self.assertEqual(result["profiles"][0]["profile_id"], "fast")
            self.assertEqual(run(api.handle("POST", "/api/config/video-profiles/fast/activate"))[0], 200)
            self.assertEqual(run(api.handle("DELETE", "/api/config/video-profiles/fast"))[0], 400)


class _FakeEngine(WorkflowEngine):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.actions = []

    async def _gen_script(self, session, instruction=""):
        return "剧本 summary"

    async def _gen_storyboard(self, session, instruction=""):
        return "分镜 summary"

    async def _gen_video(self, session, instruction=""):
        return "视频 summary"

    async def _do_publish(self, session):
        return "published"


class _FakeAdapters:
    def __init__(self):
        self.calls = []

    async def sceneforge_regenerate_shot(self, args):
        self.calls.append(("regen", args))
        return ToolResult("sceneforge_regenerate_shot", True, "{}", {"shot_idx": args.get("shot_idx")})

    async def sceneforge_publish(self, args):
        self.calls.append(("publish", args))
        return ToolResult("sceneforge_publish", True, "{}", {"url": "http://x/v.mp4"})

    def _find_final_video(self, working_dir):
        return None


class TestProductionAPI(unittest.IsolatedAsyncioTestCase):
    def _api(self, tmp):
        from services import JobRunner, ProductionService
        index = SessionIndex(tmp)
        engine = _FakeEngine(index, tmp)
        adapters = _FakeAdapters()
        service = ProductionService(engine, JobRunner(), adapters)
        return ProductionAPI(index, service, adapters), index, adapters

    async def _wait(self, api, job_id, timeout=5.0):
        waited = 0.0
        while waited < timeout:
            _, job = await api.handle("GET", f"/api/production/jobs/{job_id}")
            if job and job.get("state") != "running":
                return job
            await asyncio.sleep(0.02)
            waited += 0.02
        raise AssertionError("job did not finish in time")

    async def test_topic_approve_regenerate_via_background_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            api, index, adapters = self._api(tmp)

            status, rec = await api.handle("POST", "/api/production/topic", {"idea": "王云宝修仙"})
            self.assertEqual(status, 200)
            self.assertTrue(rec["accepted"])
            job = await self._wait(api, rec["job_id"])
            self.assertEqual(job["state"], "done")
            sid = job["result"]["session_id"]
            self.assertEqual(job["result"]["stage"], "script")

            _, snap = await api.handle("GET", "/api/production/" + sid)
            self.assertEqual(snap["stage"], "script_review_pending")
            self.assertFalse(snap["busy"])

            _, rec2 = await api.handle("POST", f"/api/production/{sid}/approve")
            job2 = await self._wait(api, rec2["job_id"])
            self.assertEqual(job2["result"]["stage"], "storyboard")

            _, rec3 = await api.handle("POST", f"/api/production/{sid}/regenerate-shot", {"shot_idx": 2})
            await self._wait(api, rec3["job_id"])
            self.assertTrue(any(c[0] == "regen" for c in adapters.calls))

    async def test_review_content_endpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            api, index, _ = self._api(tmp)
            rec = index.create(idea="王云宝")
            sid = rec["session_id"]
            _seed_idea_layout(str(index.working_dir(sid)))

            _, script = await api.handle("GET", f"/api/production/{sid}/script")
            self.assertEqual(len(script["scenes"]), 2)
            _, sb = await api.handle("GET", f"/api/production/{sid}/storyboard")
            self.assertEqual(sb["scenes"][0]["shots"][0]["visual_desc"], "雨夜小巷")
            _, man = await api.handle("GET", f"/api/production/{sid}/artifacts")
            relpath = man["scenes"][0]["shots"][0]["media"]["first_frame.png"]
            metrics_status, metrics = await api.handle(
                "GET", f"/api/production/{sid}/metrics"
            )
            self.assertEqual(metrics_status, 200)
            self.assertEqual(metrics["session_id"], sid)
            self.assertIn("cost", metrics["summary"])

            accept_status, accepted = await api.handle(
                "POST",
                f"/api/production/{sid}/review-shots/accept",
                {"shots": [{"scene_index": 0, "shot_idx": 0}]},
            )
            self.assertEqual(accept_status, 200)
            self.assertEqual(accepted["accepted_count"], 1)
            _, accepted_metrics = await api.handle(
                "GET", f"/api/production/{sid}/metrics"
            )
            self.assertEqual(accepted_metrics["summary"]["accepted_shots"], 1)

            status, fileresp = await api.handle("GET", f"/api/production/{sid}/file?path={relpath}")
            self.assertEqual(status, 200)
            self.assertIn("_file", fileresp)
            # traversal guarded
            self.assertEqual((await api.handle("GET", f"/api/production/{sid}/file?path=../../x"))[0], 404)

    async def test_manual_script_edit_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            api, index, _ = self._api(tmp)
            rec = index.create(idea="王云宝")
            sid = rec["session_id"]
            index.update_stage(sid, "script_review_pending", "x")

            status, result = await api.handle(
                "PUT", f"/api/production/{sid}/script", {"text": "场景一：办公室\n王云宝保存了代码。"}
            )

            self.assertEqual(status, 200)
            self.assertTrue(result["ok"])
            _, script = await api.handle("GET", f"/api/production/{sid}/script")
            self.assertEqual(script["story"], "场景一：办公室\n王云宝保存了代码。")

    async def test_missing_idea_and_unknown_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            api, index, _ = self._api(tmp)
            self.assertEqual((await api.handle("POST", "/api/production/topic", {}))[0], 400)
            self.assertEqual((await api.handle("GET", "/api/production/ghost"))[0], 404)
            self.assertEqual((await api.handle("GET", "/api/production/jobs/nope"))[0], 404)

    async def test_cancelled_job_is_a_user_status_not_an_error(self):
        runner = SimpleNamespace(last_job=lambda _sid: {
            "job_type": "workflow.approve",
            "state": "failed",
            "internal_state": "canceled",
            "error": "Canceled",
        })
        api = ProductionAPI(SimpleNamespace(), SimpleNamespace(runner=runner), _FakeAdapters())

        self.assertIsNone(api._last_error("project", busy=False))
        self.assertTrue(api._last_cancelled("project", busy=False))
        self.assertEqual(api._last_cancelled_job_type("project", busy=False), "workflow.approve")

    async def test_artifact_version_history_file_and_rollback_routes(self):
        from pathlib import Path

        from agent_runtime.session_factory import create_session_index
        from domain.artifacts import ArtifactType
        from services import JobRunner, ProductionService

        with tempfile.TemporaryDirectory() as tmp:
            index = create_session_index(tmp, auto_import_legacy=False)
            engine = _FakeEngine(index, tmp)
            adapters = _FakeAdapters()
            service = ProductionService(engine, JobRunner(), adapters)
            api = ProductionAPI(index, service, adapters)
            sid = index.create(idea="versioned")['session_id']
            live = Path(tmp) / ".working_dir" / sid / "idea2video" / "scene_0" / "shots" / "0" / "video.mp4"
            live.parent.mkdir(parents=True, exist_ok=True)
            live.write_bytes(b"v1")
            first = engine.artifact_versions.record_file(
                sid, 0, 0, ArtifactType.VIDEO, live, input_values={"prompt": "one"})
            live.write_bytes(b"v2")
            second = engine.artifact_versions.record_file(
                sid, 0, 0, ArtifactType.VIDEO, live, input_values={"prompt": "two"})

            rebuild_calls = []

            async def rebuild(session_id):
                rebuild_calls.append(session_id)
                return {"rebuilt": True}

            engine.rebuild_after_shot_regeneration = rebuild

            status, history = await api.handle(
                "GET",
                f"/api/production/{sid}/artifact-versions?scene_index=0&shot_index=0&artifact_type=video",
            )
            self.assertEqual(status, 200)
            self.assertEqual([item["version"] for item in history["versions"]], [2, 1])

            status, file_response = await api.handle(
                "GET", f"/api/production/{sid}/artifact-versions/{first.artifact_id}/file")
            self.assertEqual(status, 200)
            self.assertEqual(Path(file_response["_file"]).read_bytes(), b"v1")

            status, annotation = await api.handle(
                "POST",
                f"/api/production/{sid}/artifact-versions/{first.artifact_id}/annotations",
                {"text": "动作在这里跳变", "timecode_seconds": 2.4, "author": "审核员"},
            )
            self.assertEqual(status, 201)
            self.assertEqual(annotation["annotation"]["timecode_seconds"], 2.4)
            status, annotations = await api.handle(
                "GET", f"/api/production/{sid}/artifact-versions/{first.artifact_id}/annotations")
            self.assertEqual(status, 200)
            self.assertEqual(annotations["annotations"][0]["text"], "动作在这里跳变")

            status, restored = await api.handle(
                "POST",
                f"/api/production/{sid}/artifact-versions/restore-previous",
                {"shots": [{"scene_index": 0, "shot_idx": 0}], "artifact_type": "video"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(restored["restored_count"], 1)
            self.assertEqual(rebuild_calls, [sid])
            self.assertEqual(live.read_bytes(), b"v1")

            status, result = await api.handle(
                "POST", f"/api/production/{sid}/artifact-versions/{second.artifact_id}/rollback")
            self.assertEqual(status, 200)
            self.assertTrue(result["ok"])
            self.assertEqual(live.read_bytes(), b"v2")


def _seed_idea_layout(wd):
    import json as _json
    idea = os.path.join(wd, "idea2video")
    os.makedirs(os.path.join(idea, "scene_0", "shots", "0"), exist_ok=True)
    open(os.path.join(idea, "story.txt"), "w", encoding="utf-8").write("从前有个王云宝。")
    _json.dump(["场景一剧本", "场景二剧本"], open(os.path.join(idea, "script.json"), "w", encoding="utf-8"), ensure_ascii=False)
    _json.dump([{"idx": 0, "identifier_in_scene": "王云宝"}], open(os.path.join(idea, "characters.json"), "w", encoding="utf-8"), ensure_ascii=False)
    _json.dump([{"idx": 0, "cam_idx": 0, "visual_desc": "雨夜小巷", "audio_desc": "[Speaker] 王云宝: 命数已尽", "is_last": True}],
               open(os.path.join(idea, "scene_0", "storyboard.json"), "w", encoding="utf-8"), ensure_ascii=False)
    open(os.path.join(idea, "scene_0", "shots", "0", "first_frame.png"), "wb").write(b"PNG")
    open(os.path.join(idea, "scene_0", "shots", "0", "video.mp4"), "wb").write(b"MP4")
    open(os.path.join(idea, "final_video.mp4"), "wb").write(b"FINAL")


class TestArtifactsReader(unittest.TestCase):
    def test_read_script_storyboard_manifest_and_file(self):
        from server.artifacts_reader import build_manifest, read_script, read_storyboard, resolve_file
        from pathlib import Path
        with tempfile.TemporaryDirectory() as wd:
            _seed_idea_layout(wd)
            script = read_script(Path(wd))
            self.assertIn("王云宝", script["story"])
            self.assertEqual(len(script["scenes"]), 2)
            self.assertEqual(len(script["characters"]), 1)

            sb = read_storyboard(Path(wd))
            self.assertEqual(sb["scenes"][0]["shots"][0]["visual_desc"], "雨夜小巷")

            # per-shot quality verdict (when the critic wrote quality.json) rides along
            import json as _json
            _json.dump({"0": {"ok": False, "score": 0.4, "dims": {"aesthetic": 0.4}, "failed": ["aesthetic"]}},
                       open(os.path.join(wd, "idea2video", "scene_0", "quality.json"), "w", encoding="utf-8"))

            man = build_manifest(Path(wd))
            self.assertTrue(man["final_video"].endswith("final_video.mp4"))
            shot0 = man["scenes"][0]["shots"][0]
            media = shot0["media"]
            self.assertIn("first_frame.png", media)
            self.assertEqual(shot0["quality"]["failed"], ["aesthetic"])  # quality attached
            self.assertIs(shot0["quality"]["ok"], False)
            self.assertIsNotNone(resolve_file(Path(wd), media["first_frame.png"]))
            self.assertIsNone(resolve_file(Path(wd), "../../etc/passwd"))  # traversal guarded


class _Stub:
    def __init__(self, tag):
        self.tag = tag

    async def handle(self, method, path, body=None):
        return 200, {"tag": self.tag}


class TestAuthorized(unittest.TestCase):
    def test_disabled_when_no_token(self):
        from server.app import authorized
        self.assertTrue(authorized({}, None))
        self.assertTrue(authorized({"Authorization": "Bearer anything"}, ""))

    def test_bearer_and_x_auth_token(self):
        from server.app import authorized
        self.assertTrue(authorized({"Authorization": "Bearer secret"}, "secret"))
        self.assertTrue(authorized({"authorization": "Bearer secret"}, "secret"))  # case-insensitive header
        self.assertTrue(authorized({"X-Auth-Token": "secret"}, "secret"))
        self.assertFalse(authorized({"Authorization": "Bearer wrong"}, "secret"))
        self.assertFalse(authorized({}, "secret"))


class TestAppRouting(unittest.TestCase):
    def test_dispatch_and_static(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "index.html"), "w", encoding="utf-8") as f:
                f.write("<h1>hi</h1>")
            app = AppAPI(config_api=_Stub("cfg"), character_api=_Stub("char"), production_api=_Stub("prod"), static_dir=root)

            self.assertEqual(run(app.handle("GET", "/api/config"))[1]["tag"], "cfg")
            self.assertEqual(run(app.handle("GET", "/api/characters"))[1]["tag"], "char")
            self.assertEqual(run(app.handle("POST", "/api/production/topic"))[1]["tag"], "prod")
            self.assertEqual(run(app.handle("GET", "/api/unknown"))[0], 404)

            status, result = run(app.handle("GET", "/"))
            self.assertEqual(status, 200)
            self.assertTrue(result["_file"].endswith("index.html"))
            self.assertEqual(run(app.handle("GET", "/nope.js"))[0], 404)

    def test_main_server_requires_vue_build_without_legacy_fallback(self):
        from main_server import _static_dir

        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            legacy = base / "webui"
            legacy.mkdir()
            (legacy / "index.html").write_text("legacy", encoding="utf-8")

            with self.assertRaisesRegex(FileNotFoundError, "Vue frontend build not found"):
                _static_dir(base)

            dist = base / "webui-dist"
            dist.mkdir()
            (dist / "index.html").write_text("vue", encoding="utf-8")
            self.assertEqual(_static_dir(base), dist)


if __name__ == "__main__":
    unittest.main()
