"""Tests for the content moderation gate."""

import unittest

from services.moderation import ContentModerator


class TestModerator(unittest.TestCase):
    def test_disabled_allows_everything(self):
        m = ContentModerator(enabled=False, keywords=["bad"])
        self.assertTrue(m.check("this is bad")["ok"])

    def test_keyword_blocks(self):
        m = ContentModerator(enabled=True, keywords=["涉政", "暴恐"])
        v = m.check("一个涉政主题的视频")
        self.assertFalse(v["ok"])
        self.assertTrue(v["flagged"])
        self.assertEqual(v["matched"], ["涉政"])

    def test_clean_passes(self):
        m = ContentModerator(enabled=True, keywords=["涉政"])
        self.assertTrue(m.check("程序员王云宝的逆袭")["ok"])

    def test_empty_text_ok(self):
        self.assertTrue(ContentModerator(enabled=True, keywords=["x"]).check("")["ok"])

    def test_from_config(self):
        m = ContentModerator.from_config({"moderation": {"enabled": True, "keywords": ["A", "B"]}})
        self.assertTrue(m.enabled)
        self.assertEqual(m.keywords, ["a", "b"])

    def test_cloud_provider_fails_open_marked(self):
        m = ContentModerator(enabled=True, provider="cloud")
        v = m.check("anything")
        self.assertTrue(v["ok"])  # fails open
        self.assertIn("未接入", v["reason"])


if __name__ == "__main__":
    unittest.main()
