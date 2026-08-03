"""Tests for the LLM hook-line generator (HookWriter)."""

import asyncio
import unittest
from types import SimpleNamespace

from agents.hook_writer import HookWriter, _clean


class _FakeModel:
    def __init__(self, content):
        self.content = content
        self.prompt = None

    async def ainvoke(self, prompt):
        self.prompt = prompt
        return SimpleNamespace(content=self.content)


class TestClean(unittest.TestCase):
    def test_strips_quotes_and_prefix(self):
        self.assertEqual(_clean('钩子文案：“被裁那天，他还不知道”'), "被裁那天，他还不知道")
        self.assertEqual(_clean('《三年后他归来》'), "三年后他归来")
        self.assertEqual(_clean("Hook: He didn't know yet"), "He didn't know yet")

    def test_takes_first_nonempty_line(self):
        self.assertEqual(_clean("\n\n第一句\n第二句"), "第一句")


class TestGenerate(unittest.TestCase):
    def test_generate_returns_cleaned_line(self):
        model = _FakeModel("被裁那天，他还不知道\n")
        out = asyncio.run(HookWriter(model).generate("程序员王云宝被裁员后创业", chinese=True))
        self.assertEqual(out, "被裁那天，他还不知道")
        self.assertIn("程序员王云宝", model.prompt)  # source fed into prompt

    def test_empty_source_returns_empty(self):
        self.assertEqual(asyncio.run(HookWriter(_FakeModel("x")).generate("", chinese=True)), "")

    def test_none_model_returns_empty(self):
        self.assertEqual(asyncio.run(HookWriter(None).generate("something", chinese=True)), "")

    def test_english_prompt_when_not_chinese(self):
        model = _FakeModel("He didn't know yet")
        asyncio.run(HookWriter(model).generate("a programmer's comeback", chinese=False))
        self.assertIn("hook copywriter", model.prompt.lower())


class _SeqModel:
    """Returns a queued reply per ainvoke call (for multi-step best-of-N)."""
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    async def ainvoke(self, prompt):
        self.prompts.append(prompt)
        return SimpleNamespace(content=self.replies.pop(0))


class TestCandidates(unittest.TestCase):
    def test_generate_candidates_parses_numbered(self):
        m = _FakeModel("1. 被裁那天他还不知道\n2. 三年后他归来\n3. 谁也没想到结局")
        cands = asyncio.run(HookWriter(m).generate_candidates("程序员逆袭", n=3, chinese=True))
        self.assertEqual(cands, ["被裁那天他还不知道", "三年后他归来", "谁也没想到结局"])

    def test_candidates_n1_uses_single(self):
        m = _FakeModel("就一条钩子")
        self.assertEqual(asyncio.run(HookWriter(m).generate_candidates("x", n=1)), ["就一条钩子"])

    def test_select_best_picks_by_number(self):
        m = _FakeModel("我选 2")
        chosen = asyncio.run(HookWriter(m).select_best(["A", "B", "C"]))
        self.assertEqual(chosen, "B")

    def test_select_best_fallback_first_on_garbage(self):
        m = _FakeModel("无法确定")
        self.assertEqual(asyncio.run(HookWriter(m).select_best(["A", "B"])), "A")

    def test_best_end_to_end(self):
        # call 1: candidates ; call 2: pick #3
        m = _SeqModel(["1. aaa\n2. bbb\n3. ccc", "3"])
        res = asyncio.run(HookWriter(m).best("源", n=3, chinese=True))
        self.assertEqual(res["candidates"], ["aaa", "bbb", "ccc"])
        self.assertEqual(res["chosen"], "ccc")


if __name__ == "__main__":
    unittest.main()
