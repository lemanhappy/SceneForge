"""Tests for domain-specific reasoning packs (领域化推理).

Covers pack resolution (keys, aliases, unknown/empty -> no-op general pack),
instruction composition with a base instruction, the UI/config listing, and the
HookWriter domain-instruction injection.
"""

import asyncio
import unittest
from types import SimpleNamespace

from agents.domain_packs import DomainPack, resolve_domain, list_domains
from agents.hook_writer import HookWriter


class TestResolve(unittest.TestCase):
    def test_known_key(self):
        self.assertEqual(resolve_domain("short_drama").key, "short_drama")
        self.assertEqual(resolve_domain("explainer").key, "explainer")
        self.assertEqual(resolve_domain("knowledge").key, "knowledge")
        self.assertEqual(resolve_domain("product").key, "product")

    def test_aliases(self):
        self.assertEqual(resolve_domain("短剧").key, "short_drama")
        self.assertEqual(resolve_domain("影视解说").key, "explainer")
        self.assertEqual(resolve_domain("科普").key, "knowledge")
        self.assertEqual(resolve_domain("商品展示").key, "product")
        self.assertEqual(resolve_domain("DRAMA").key, "short_drama")  # case-insensitive

    def test_empty_and_unknown_fall_back_to_general(self):
        for v in ("", None, "general", "none", "default", "通用", "完全没听过的领域"):
            self.assertEqual(resolve_domain(v).key, "general")

    def test_general_pack_is_noop(self):
        pack = resolve_domain("")
        self.assertEqual(pack.screenwriter, "")
        self.assertEqual(pack.storyboard, "")
        self.assertEqual(pack.hook, "")


class TestInstruction(unittest.TestCase):
    def test_compose_with_base(self):
        pack = resolve_domain("short_drama")
        out = pack.instruction_for("screenwriter", "请用中文。")
        self.assertIn("请用中文。", out)
        self.assertIn("爽点前置", out)
        # base comes first, snippet after a blank line
        self.assertTrue(out.startswith("请用中文。"))

    def test_compose_empty_base_returns_snippet(self):
        pack = resolve_domain("short_drama")
        self.assertEqual(pack.instruction_for("storyboard", ""), pack.storyboard.strip())

    def test_general_compose_returns_base_only(self):
        pack = resolve_domain("")
        self.assertEqual(pack.instruction_for("screenwriter", "BASE"), "BASE")

    def test_unknown_role_is_safe(self):
        # getattr on a missing role returns "" -> base only
        pack = resolve_domain("short_drama")
        self.assertEqual(pack.instruction_for("nonexistent_role", "BASE"), "BASE")


class TestListing(unittest.TestCase):
    def test_general_first_then_builtins(self):
        items = list_domains()
        keys = [d["key"] for d in items]
        self.assertEqual(keys[0], "general")
        for k in ("short_drama", "explainer", "knowledge", "product"):
            self.assertIn(k, keys)
        self.assertTrue(all("label" in d for d in items))


class _FakeModel:
    def __init__(self, content):
        self.content = content
        self.prompt = None

    async def ainvoke(self, prompt):
        self.prompt = prompt
        return SimpleNamespace(content=self.content)


class TestHookInjection(unittest.TestCase):
    def test_domain_instruction_appended_to_prompt(self):
        snippet = resolve_domain("short_drama").hook
        model = _FakeModel("被裁那天他还不知道")
        out = asyncio.run(HookWriter(model, extra_instruction=snippet).generate("程序员逆袭", chinese=True))
        self.assertEqual(out, "被裁那天他还不知道")
        self.assertIn("领域要求", model.prompt)
        self.assertIn(snippet[:8], model.prompt)

    def test_no_instruction_leaves_prompt_unchanged(self):
        model = _FakeModel("x")
        asyncio.run(HookWriter(model).generate("y", chinese=True))
        self.assertNotIn("领域要求", model.prompt)

    def test_product_pack_has_generation_rules(self):
        pack = resolve_domain("product")
        self.assertIn("卖点证明", pack.screenwriter)
        self.assertIn("商品始终是视觉主体", pack.storyboard)
        self.assertIn("product photography", pack.video)


class TestPipelineWiring(unittest.TestCase):
    def test_storyboard_artist_gets_domain_snippet(self):
        from pipelines.script2video_pipeline import Script2VideoPipeline
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            pipe = Script2VideoPipeline(
                chat_model=None, image_generator=None, video_generator=None,
                working_dir=d, chinese_instruction="请用中文。", domain="short_drama",
            )
            self.assertEqual(pipe._domain_pack.key, "short_drama")
            instr = pipe.storyboard_artist.extra_system_instruction
            self.assertIn("请用中文。", instr)
            self.assertIn("反应镜头", instr)

    def test_screenwriter_gets_domain_snippet(self):
        from pipelines.idea2video_pipeline import Idea2VideoPipeline
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            pipe = Idea2VideoPipeline(
                chat_model=None, image_generator=None, video_generator=None,
                working_dir=d, chinese_instruction="请用中文。", domain="explainer",
            )
            self.assertEqual(pipe._domain_pack.key, "explainer")
            self.assertIn("第三人称解说", pipe.screenwriter.extra_system_instruction)

    def test_default_domain_is_noop(self):
        from pipelines.script2video_pipeline import Script2VideoPipeline
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            pipe = Script2VideoPipeline(
                chat_model=None, image_generator=None, video_generator=None,
                working_dir=d, chinese_instruction="请用中文。",
            )
            self.assertEqual(pipe._domain_pack.key, "general")
            # storyboard instruction is exactly the Chinese instruction, no domain text
            self.assertEqual(pipe.storyboard_artist.extra_system_instruction, "请用中文。")


if __name__ == "__main__":
    unittest.main()
