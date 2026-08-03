"""Tests for the character studio: registry persistence, studio service, API."""

import asyncio
import os
import tempfile
import unittest

from characters import CharacterAsset, CharacterAssetRegistry
from characters.studio import CharacterStudio
from server import CharacterStudioAPI


def run(coro):
    return asyncio.run(coro)


class _FakeImage:
    def __init__(self, prompt):
        self.prompt = prompt

    def save(self, path):
        with open(path, "w", encoding="utf-8") as f:
            f.write("img:" + self.prompt[:30])


class _FakeImageGen:
    def __init__(self):
        self.calls = []

    async def generate_single_image(self, prompt, reference_image_paths=None, **kwargs):
        self.calls.append({"prompt": prompt, "refs": list(reference_image_paths or [])})
        return _FakeImage(prompt)


class TestRegistryPersistence(unittest.TestCase):
    def test_save_upsert_remove_roundtrip(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "registry.yaml")
            reg = CharacterAssetRegistry.open_or_create(path)
            self.assertEqual(len(reg), 0)
            img = os.path.join(root, "lin", "front.png")
            os.makedirs(os.path.dirname(img), exist_ok=True)
            open(img, "w").write("x")
            reg.upsert(CharacterAsset(asset_id="lin", display_name="林老师", description="老师", assets={"front": img}))
            reg.save()

            reloaded = CharacterAssetRegistry.from_yaml(path)
            asset = reloaded.get("lin")
            self.assertIsNotNone(asset)
            self.assertEqual(asset.display_name, "林老师")
            # path persisted relative, resolved back to an existing absolute file
            self.assertTrue(os.path.exists(asset.assets["front"]))

            reloaded.remove("lin")
            reloaded.save()
            self.assertEqual(len(CharacterAssetRegistry.from_yaml(path)), 0)


class TestCharacterStudio(unittest.TestCase):
    def test_upsert_list_get(self):
        with tempfile.TemporaryDirectory() as root:
            studio = CharacterStudio(os.path.join(root, "registry.yaml"), _FakeImageGen())
            studio.upsert("wangyunbao", display_name="王云宝", description="落魄算命青年，混沌青莲转世", aliases=["云宝", "主角"])
            self.assertEqual(len(studio.list_characters()), 1)
            c = studio.get("wangyunbao")
            self.assertEqual(c["display_name"], "王云宝")
            self.assertIn("云宝", c["aliases"])
            self.assertEqual(c["views"], {})

    def test_generate_views_and_iterate(self):
        with tempfile.TemporaryDirectory() as root:
            gen = _FakeImageGen()
            studio = CharacterStudio(os.path.join(root, "registry.yaml"), gen, assets_root=root)
            studio.upsert("wyb", display_name="王云宝", visual_prompt="young man, glowing eyes")

            # side before front -> error
            with self.assertRaises(ValueError):
                run(studio.generate_view("wyb", view="side"))

            front = run(studio.generate_view("wyb", view="front", style="dark gold xianxia"))
            self.assertTrue(os.path.exists(front["path"]))
            self.assertIn("front", studio.get("wyb")["views"])
            self.assertEqual(
                studio.registry.get("wyb").default_reference_set().images["front"],
                front["path"],
            )
            self.assertEqual(gen.calls[-1]["refs"], [])  # front uses no reference

            side = run(studio.generate_view("wyb", view="side"))
            self.assertTrue(os.path.exists(side["path"]))
            self.assertEqual(gen.calls[-1]["refs"], [front["path"]])  # side references front

            # iterate ("优化"): regenerate front with an extra prompt
            run(studio.generate_view("wyb", view="front", extra_prompt="more determined expression"))
            self.assertIn("more determined", gen.calls[-1]["prompt"])

            # persisted: a fresh registry sees the views -> usable as a fixed asset
            reg = CharacterAssetRegistry.from_yaml(os.path.join(root, "registry.yaml"))
            self.assertEqual(set(reg.get("wyb").assets.keys()), {"front", "side"})

    def test_generate_unknown_character_raises(self):
        with tempfile.TemporaryDirectory() as root:
            studio = CharacterStudio(os.path.join(root, "registry.yaml"), _FakeImageGen())
            with self.assertRaises(KeyError):
                run(studio.generate_view("ghost", view="front"))


class TestCharacterStudioAPI(unittest.TestCase):
    def _api(self, root):
        studio = CharacterStudio(os.path.join(root, "registry.yaml"), _FakeImageGen(), assets_root=root)
        return CharacterStudioAPI(studio)

    def test_full_api_flow(self):
        with tempfile.TemporaryDirectory() as root:
            api = self._api(root)

            status, body = run(api.handle("GET", "/api/characters"))
            self.assertEqual(status, 200)
            self.assertEqual(body["characters"], [])

            status, body = run(api.handle("POST", "/api/characters", {"asset_id": "wyb", "display_name": "王云宝", "visual_prompt": "young man"}))
            self.assertEqual(status, 200)
            self.assertEqual(body["display_name"], "王云宝")

            status, body = run(api.handle("GET", "/api/characters/wyb"))
            self.assertEqual(status, 200)
            self.assertEqual(body["asset_id"], "wyb")

            status, body = run(api.handle("POST", "/api/characters/wyb/generate", {"view": "front", "style": "xianxia"}))
            self.assertEqual(status, 200)
            self.assertEqual(body["view"], "front")

            status, body = run(api.handle("GET", "/api/characters/wyb/image/front"))
            self.assertEqual(status, 200)
            self.assertIn("_file", body)
            self.assertTrue(os.path.exists(body["_file"]))

            status, body = run(api.handle("DELETE", "/api/characters/wyb"))
            self.assertEqual(status, 200)
            self.assertTrue(body["removed"])

            status, body = run(api.handle("GET", "/api/characters/wyb"))
            self.assertEqual(status, 404)

    def test_api_errors(self):
        with tempfile.TemporaryDirectory() as root:
            api = self._api(root)
            self.assertEqual(run(api.handle("GET", "/api/nope"))[0], 404)
            self.assertEqual(run(api.handle("POST", "/api/characters", {}))[0], 400)  # missing asset_id
            self.assertEqual(run(api.handle("POST", "/api/characters/ghost/generate", {"view": "front"}))[0], 404)
            self.assertEqual(run(api.handle("GET", "/api/characters/x/image/front"))[0], 404)

    def test_identity_and_lora_binding_are_optional_api_fields(self):
        with tempfile.TemporaryDirectory() as root:
            api = self._api(root)
            status, body = run(api.handle("POST", "/api/characters", {
                "asset_id": "lead",
                "display_name": "Lead",
                "identity_profile": {
                    "facial_features": "oval face",
                    "forbidden_changes": ["eye color"],
                },
                "bible": {
                    "personality_traits": ["restrained"],
                    "continuity_notes": "scar remains above the left eyebrow",
                    "voice": {"vocal_quality": "low and calm"},
                },
                "render_bindings": [{
                    "kind": "lora",
                    "binding_id": "lead_lora",
                    "model_path": "cloud:model/lead",
                    "trigger_words": ["lead_person"],
                }],
            }))

            self.assertEqual(status, 200)
            self.assertEqual(body["identity_profile"]["facial_features"], "oval face")
            self.assertEqual(body["bible"]["voice"]["vocal_quality"], "low and calm")
            self.assertFalse(body["render_bindings"][0]["enabled"])
            self.assertEqual(body["enabled_render_bindings"], [])


if __name__ == "__main__":
    unittest.main()
