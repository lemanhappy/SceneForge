"""Tests for character portrait version history (archive on regenerate + rollback)."""

import asyncio
import os
import tempfile
import unittest

from characters.studio import CharacterStudio


class _FakeImage:
    def __init__(self, tag):
        self.tag = tag

    def save(self, path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.tag)


class _FakeGen:
    def __init__(self):
        self.n = 0

    async def generate_single_image(self, prompt, reference_image_paths=None, **kw):
        self.n += 1
        return _FakeImage(f"img{self.n}")


def _studio(d):
    reg = os.path.join(d, "registry.yaml")
    studio = CharacterStudio(reg, _FakeGen(), assets_root=d)
    studio.upsert("hero", display_name="Hero", description="a hero")
    return studio


class TestVersions(unittest.TestCase):
    def test_regenerate_archives_previous(self):
        with tempfile.TemporaryDirectory() as d:
            s = _studio(d)
            asyncio.run(s.generate_view("hero", "front"))   # img1, no archive (first)
            self.assertEqual(s.list_versions("hero", "front"), [])
            asyncio.run(s.generate_view("hero", "front"))   # img2, archives img1 as v1
            vers = s.list_versions("hero", "front")
            self.assertEqual([v["version"] for v in vers], [1])
            asyncio.run(s.generate_view("hero", "front"))   # img3, archives img2 as v2
            self.assertEqual([v["version"] for v in s.list_versions("hero", "front")], [2, 1])  # newest first
            # current file holds the latest render
            cur = os.path.join(d, "hero", "front.png")
            self.assertEqual(open(cur, encoding="utf-8").read(), "img3")

    def test_rollback_restores_and_archives_current(self):
        with tempfile.TemporaryDirectory() as d:
            s = _studio(d)
            asyncio.run(s.generate_view("hero", "front"))   # img1
            asyncio.run(s.generate_view("hero", "front"))   # img2, v1=img1
            res = s.rollback("hero", "front", 1)            # restore img1; archive img2 as v2
            self.assertEqual(res["restored_from"], 1)
            cur = os.path.join(d, "hero", "front.png")
            self.assertEqual(open(cur, encoding="utf-8").read(), "img1")
            # rollback archived the pre-rollback current (img2) as a new version
            self.assertEqual([v["version"] for v in s.list_versions("hero", "front")], [2, 1])

    def test_rollback_missing_version_errors(self):
        with tempfile.TemporaryDirectory() as d:
            s = _studio(d)
            asyncio.run(s.generate_view("hero", "front"))
            with self.assertRaises(ValueError):
                s.rollback("hero", "front", 99)

    def test_version_path(self):
        with tempfile.TemporaryDirectory() as d:
            s = _studio(d)
            asyncio.run(s.generate_view("hero", "front"))
            asyncio.run(s.generate_view("hero", "front"))
            self.assertTrue(s.version_path("hero", "front", 1).endswith("v1.png"))
            self.assertIsNone(s.version_path("hero", "front", 5))


if __name__ == "__main__":
    unittest.main()
