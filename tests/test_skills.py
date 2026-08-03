"""Tests for user-uploaded skills: Markdown parsing, the on-disk registry +
resolve_domain integration, and the SkillsService upload/list/delete + guards."""

import asyncio
import tempfile
import unittest
from pathlib import Path

from agents.domain_packs import (
    parse_skill_markdown,
    load_skills_from_dir,
    list_user_skills,
    resolve_domain,
)
from server.skills_service import SkillsService
from server.skills_api import SkillsAPI


_SKILL_MD = """---
name: 赛博朋克悬疑短剧
key: cyberpunk_drama
author: sam
---

## 剧本
高概念反乌托邦设定，台词冷硬。

## 分镜
大量低角度、霓虹光比、雨夜街景。

## 视频
cyberpunk, neon-lit, rain, cinematic

## 钩子
用一句反乌托邦冷启动。
"""


class TestSkillParsing(unittest.TestCase):
    def test_parses_frontmatter_and_sections(self):
        pack = parse_skill_markdown(_SKILL_MD)
        self.assertIsNotNone(pack)
        self.assertEqual(pack.key, "cyberpunk_drama")
        self.assertEqual(pack.label, "赛博朋克悬疑短剧")
        self.assertEqual(pack.author, "sam")
        self.assertIn("反乌托邦", pack.screenwriter)
        self.assertIn("霓虹", pack.storyboard)
        self.assertIn("cyberpunk", pack.video)
        self.assertIn("冷启动", pack.hook)

    def test_english_headings_and_no_frontmatter(self):
        pack = parse_skill_markdown(
            "## Script\nA tense thriller.\n\n## Video\nfilm noir, high contrast",
            default_key="noir")
        self.assertIsNotNone(pack)
        self.assertEqual(pack.key, "noir")
        self.assertIn("thriller", pack.screenwriter)
        self.assertIn("film noir", pack.video)
        # untouched roles stay empty
        self.assertEqual(pack.storyboard, "")
        self.assertEqual(pack.hook, "")

    def test_key_sanitized_from_filename(self):
        pack = parse_skill_markdown("## 视频\nstyle", default_key="My Cool Skill!!")
        self.assertEqual(pack.key, "my_cool_skill")

    def test_empty_or_garbage_rejected(self):
        self.assertIsNone(parse_skill_markdown(""))
        self.assertIsNone(parse_skill_markdown("just some prose with no ## sections"))
        # only-unknown sections -> no usable content
        self.assertIsNone(parse_skill_markdown("## 备注\nnothing useful here"))


class TestRegistryIntegration(unittest.TestCase):
    def test_load_dir_and_resolve(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "cyberpunk_drama.md").write_text(_SKILL_MD, encoding="utf-8")
            (Path(d) / "bad.md").write_text("no sections", encoding="utf-8")
            n = load_skills_from_dir(d)
            self.assertEqual(n, 1)  # bad.md skipped
            keys = {p.key for p in list_user_skills()}
            self.assertEqual(keys, {"cyberpunk_drama"})
            pack = resolve_domain("cyberpunk_drama")
            self.assertIn("cyberpunk", pack.video)
            # builtin domains still resolve; unknown -> general no-op
            self.assertEqual(resolve_domain("short_drama").key, "short_drama")
            self.assertEqual(resolve_domain("nope").key, "general")

    def test_missing_dir_clears_registry(self):
        load_skills_from_dir(_SKILL_MD)  # not a dir
        self.assertEqual(list_user_skills(), [])


class TestSkillsService(unittest.TestCase):
    def _svc(self, root):
        return SkillsService(workspace_root=root, config_paths=[])

    def test_upload_list_delete(self):
        with tempfile.TemporaryDirectory() as root:
            svc = self._svc(root)
            r = svc.upload("whatever.md", _SKILL_MD)
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["skill"]["key"], "cyberpunk_drama")
            self.assertTrue((Path(root) / "skills_user" / "cyberpunk_drama.md").exists())
            listed = svc.list()
            self.assertEqual([s["key"] for s in listed["skills"]], ["cyberpunk_drama"])
            self.assertEqual(listed["market_url"], "")
            # resolvable by the pipeline after upload
            self.assertIn("cyberpunk", resolve_domain("cyberpunk_drama").video)
            d = svc.delete("cyberpunk_drama")
            self.assertTrue(d["ok"])
            self.assertFalse((Path(root) / "skills_user" / "cyberpunk_drama.md").exists())
            self.assertEqual(svc.list()["skills"], [])

    def test_upload_rejections(self):
        with tempfile.TemporaryDirectory() as root:
            svc = self._svc(root)
            self.assertFalse(svc.upload("x.md", "")["ok"])              # empty
            self.assertFalse(svc.upload("x.md", "no sections here")["ok"])  # no usable content
            self.assertFalse(svc.upload("x.md", "a" * (70 * 1024))["ok"])   # oversize

    def test_upload_key_from_filename_when_no_frontmatter(self):
        with tempfile.TemporaryDirectory() as root:
            svc = self._svc(root)
            r = svc.upload("../../evil name.md", "## 视频\nstyle")
            self.assertTrue(r["ok"])
            # filename sanitized -> no path traversal, file stays inside skills_user
            self.assertEqual(r["skill"]["key"], "evil_name")
            files = list((Path(root) / "skills_user").glob("*.md"))
            self.assertEqual([f.name for f in files], ["evil_name.md"])

    def test_market_url_from_config(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = Path(root) / "c.yaml"
            cfg.write_text("skills:\n  market_url: https://example.com/skills\n", encoding="utf-8")
            svc = SkillsService(workspace_root=root, config_paths=[str(cfg)])
            data = svc.list()
            self.assertEqual(data["market_url"], "https://example.com/skills")
            # configured market leads, built-in public markets follow
            self.assertEqual(data["markets"][0]["url"], "https://example.com/skills")
            self.assertTrue(any("skills.sh" in m["url"] for m in data["markets"]))

    def test_lists_builtin_packs(self):
        with tempfile.TemporaryDirectory() as root:
            svc = SkillsService(workspace_root=root, config_paths=[])
            keys = {b["key"] for b in svc.list()["builtins"]}
            self.assertIn("short_drama", keys)  # 短剧 built-in shown read-only

    def test_fork_builtin_into_editable_skill(self):
        with tempfile.TemporaryDirectory() as root:
            svc = SkillsService(workspace_root=root, config_paths=[])
            r = svc.fork_builtin("short_drama")
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["skill"]["key"], "short_drama_custom")  # distinct key
            self.assertTrue((Path(root) / "skills_user" / "short_drama_custom.md").exists())
            # the fork carries the built-in's storyboard reasoning and is resolvable
            self.assertTrue(r["skill"]["storyboard"])
            self.assertTrue(resolve_domain("short_drama_custom").storyboard)
            # the built-in itself is unchanged (still resolves to the compiled pack)
            self.assertEqual(resolve_domain("short_drama").key, "short_drama")

    def test_fork_unknown_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            svc = SkillsService(workspace_root=root, config_paths=[])
            self.assertFalse(svc.fork_builtin("nope")["ok"])
            self.assertFalse(svc.fork_builtin("general")["ok"])

    def test_builtin_markets_present_without_config(self):
        with tempfile.TemporaryDirectory() as root:
            svc = SkillsService(workspace_root=root, config_paths=[])
            markets = svc.list()["markets"]
            self.assertTrue(markets)  # built-ins shown even with no config
            self.assertTrue(all(m.get("url", "").startswith("http") for m in markets))


class TestSkillExamples(unittest.TestCase):
    def _svc_with_examples(self, root):
        ex = Path(root) / "skills_examples"
        ex.mkdir()
        (ex / "cyberpunk_drama.md").write_text(_SKILL_MD, encoding="utf-8")
        return SkillsService(workspace_root=root, config_paths=[])

    def test_list_includes_examples_with_installed_flag(self):
        with tempfile.TemporaryDirectory() as root:
            svc = self._svc_with_examples(root)
            data = svc.list()
            self.assertEqual([e["key"] for e in data["examples"]], ["cyberpunk_drama"])
            self.assertFalse(data["examples"][0]["installed"])
            self.assertEqual(data["skills"], [])

    def test_import_example_installs_and_marks(self):
        with tempfile.TemporaryDirectory() as root:
            svc = self._svc_with_examples(root)
            r = svc.import_example("cyberpunk_drama")
            self.assertTrue(r["ok"], r)
            self.assertTrue((Path(root) / "skills_user" / "cyberpunk_drama.md").exists())
            data = svc.list()
            self.assertEqual([s["key"] for s in data["skills"]], ["cyberpunk_drama"])
            self.assertTrue(data["examples"][0]["installed"])  # now flagged installed
            # resolvable by the pipeline
            self.assertIn("cyberpunk", resolve_domain("cyberpunk_drama").video)

    def test_import_unknown_example_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            svc = self._svc_with_examples(root)
            self.assertFalse(svc.import_example("nope")["ok"])

    def test_bundled_examples_parse(self):
        # The example files shipped in the repo must all be valid skills.
        ex_dir = Path(__file__).resolve().parent.parent / "skills_examples"
        files = list(ex_dir.glob("*.md"))
        self.assertTrue(files, "no bundled example skills found")
        for f in files:
            pack = parse_skill_markdown(f.read_text(encoding="utf-8"), default_key=f.stem)
            self.assertIsNotNone(pack, f.name)
            self.assertTrue(pack.video, f"{f.name} missing 视频 section")


class _RecordingGen:
    def __init__(self):
        self.image_prompts = []

    async def generate_single_image(self, prompt, reference_image_paths=None, **kw):
        self.image_prompts.append(prompt)
        return "img"

    async def generate_single_video(self, prompt, reference_image_paths=None, **kw):
        return "vid"


class TestSkillVideoStyleReachesPrompt(unittest.TestCase):
    def test_video_snippet_appended_to_generation_prompt(self):
        from pipelines.script2video_pipeline import Script2VideoPipeline, _PromptSuffixGenerator
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "cyberpunk_drama.md").write_text(_SKILL_MD, encoding="utf-8")
            load_skills_from_dir(d)
            # No on-screen-text constraint, only the skill's `video` style -> the
            # generator is still wrapped and the style reaches the prompt.
            pipe = Script2VideoPipeline(chat_model=object(), image_generator=_RecordingGen(),
                                        video_generator=_RecordingGen(), working_dir="/tmp/x",
                                        domain="cyberpunk_drama")
            self.assertIsInstance(pipe.image_generator, _PromptSuffixGenerator)
            inner = pipe.image_generator._inner
            asyncio.run(pipe.image_generator.generate_single_image("a street", reference_image_paths=[]))
            self.assertIn("cyberpunk", inner.image_prompts[0])
            # storyboard reasoning + hook also picked up the skill
            self.assertIn("霓虹", pipe.storyboard_artist.extra_system_instruction)
            self.assertIn("冷启动", pipe._domain_pack.hook)


class TestSkillsAPI(unittest.IsolatedAsyncioTestCase):
    async def test_routes(self):
        with tempfile.TemporaryDirectory() as root:
            api = SkillsAPI(SkillsService(workspace_root=root, config_paths=[]))
            code, body = await api.handle("POST", "/api/skills", {"filename": "s.md", "content": _SKILL_MD})
            self.assertEqual(code, 200)
            self.assertTrue(body["ok"])
            code, body = await api.handle("GET", "/api/skills")
            self.assertEqual(code, 200)
            self.assertEqual(len(body["skills"]), 1)
            code, body = await api.handle("DELETE", "/api/skills/cyberpunk_drama")
            self.assertEqual(code, 200)
            code, body = await api.handle("DELETE", "/api/skills/cyberpunk_drama")
            self.assertEqual(code, 404)  # already gone
            code, body = await api.handle("POST", "/api/skills", {"filename": "s.md", "content": ""})
            self.assertEqual(code, 400)  # rejected upload


if __name__ == "__main__":
    unittest.main()
