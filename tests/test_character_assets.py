"""Tests for fixed character asset registry and pipeline injection."""

import asyncio
import os
import tempfile
import types
import unittest

import yaml

from characters import CharacterAsset, CharacterAssetRegistry, ReferenceSet
from interfaces import CharacterInScene
from pipelines.script2video_pipeline import Script2VideoPipeline
from pipelines.idea2video_pipeline import Idea2VideoPipeline


def _write_registry(root):
    """Create a registry.yaml plus dummy image files under root, return path."""
    teacher_dir = os.path.join(root, "teacher_lin")
    os.makedirs(teacher_dir, exist_ok=True)
    for view in ("front", "side", "back"):
        with open(os.path.join(teacher_dir, f"{view}.png"), "w", encoding="utf-8") as f:
            f.write(view)
    registry = {
        "characters": {
            "teacher_lin": {
                "display_name": "林老师",
                "aliases": ["林老师", "女老师", "山村老师"],
                "type": "reference_images",
                "description": "年轻女教师，短发，白衬衫。",
                "assets": {
                    "front": "teacher_lin/front.png",
                    "side": "teacher_lin/side.png",
                    "back": "teacher_lin/back.png",
                },
            }
        }
    }
    path = os.path.join(root, "registry.yaml")
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(registry, f, allow_unicode=True)
    return path


class TestCharacterAssetRegistry(unittest.TestCase):
    def test_from_yaml_resolves_relative_paths(self):
        with tempfile.TemporaryDirectory() as root:
            path = _write_registry(root)
            reg = CharacterAssetRegistry.from_yaml(path)
            self.assertEqual(len(reg), 1)
            asset = reg.get("teacher_lin")
            self.assertIsNotNone(asset)
            self.assertEqual(asset.display_name, "林老师")
            # relative paths resolved to existing absolute files
            self.assertTrue(os.path.isabs(asset.assets["front"]))
            self.assertTrue(os.path.exists(asset.assets["front"]))

    def test_match_exact_and_alias_and_substring(self):
        with tempfile.TemporaryDirectory() as root:
            reg = CharacterAssetRegistry.from_yaml(_write_registry(root))
            self.assertEqual(reg.match("teacher_lin").asset_id, "teacher_lin")   # id
            self.assertEqual(reg.match("女老师").asset_id, "teacher_lin")          # alias
            self.assertEqual(reg.match("林老师（主角）").asset_id, "teacher_lin")   # substring
            self.assertIsNone(reg.match("校长"))                                   # no match

    def test_match_characters_builds_bindings(self):
        with tempfile.TemporaryDirectory() as root:
            reg = CharacterAssetRegistry.from_yaml(_write_registry(root))
            chars = [
                types.SimpleNamespace(identifier_in_scene="林老师", static_features="", dynamic_features=""),
                types.SimpleNamespace(identifier_in_scene="校长", static_features="", dynamic_features=""),
            ]
            self.assertEqual(reg.match_characters(chars), {"林老师": "teacher_lin"})

    def test_from_config_disabled_returns_none(self):
        self.assertIsNone(CharacterAssetRegistry.from_config({}))
        self.assertIsNone(CharacterAssetRegistry.from_config({"character_assets": {"enabled": False}}))

    def test_from_config_enabled(self):
        with tempfile.TemporaryDirectory() as root:
            path = _write_registry(root)
            reg = CharacterAssetRegistry.from_config(
                {"character_assets": {"enabled": True, "registry_path": path}}
            )
            self.assertIsNotNone(reg)
            self.assertEqual(len(reg), 1)


class TestFixedAssetInjection(unittest.TestCase):
    def _asset(self, root):
        for view in ("front", "side"):
            with open(os.path.join(root, f"{view}.jpg"), "w", encoding="utf-8") as f:
                f.write(view)
        return CharacterAsset(
            asset_id="teacher_lin",
            display_name="林老师",
            description="年轻女教师。",
            assets={"front": os.path.join(root, "front.jpg"), "side": os.path.join(root, "side.jpg")},
        )

    def test_build_fixed_registry_entry_structure_and_copy(self):
        with tempfile.TemporaryDirectory() as root:
            asset = self._asset(root)
            char_dir = os.path.join(root, "out")
            entry = Script2VideoPipeline._build_fixed_registry_entry("林老师", asset, char_dir)

            self.assertIn("林老师", entry)
            self.assertEqual(set(entry["林老师"].keys()), {"front", "side"})
            front = entry["林老师"]["front"]
            # extension preserved, file copied into the session dir
            self.assertTrue(front["path"].endswith("front.jpg"))
            self.assertTrue(os.path.exists(front["path"]))
            self.assertIn("FIXED character asset", front["description"])
            self.assertIn("年轻女教师", front["description"])

    def test_build_fixed_registry_entry_prefers_new_reference_set(self):
        with tempfile.TemporaryDirectory() as root:
            legacy = os.path.join(root, "legacy.png")
            selected = os.path.join(root, "selected.png")
            open(legacy, "w", encoding="utf-8").write("legacy")
            open(selected, "w", encoding="utf-8").write("selected")
            asset = CharacterAsset(
                asset_id="teacher_lin",
                display_name="林老师",
                assets={"front": legacy},
                reference_sets=[ReferenceSet(
                    reference_set_id="costume_b",
                    images={"front": selected},
                    is_default=True,
                )],
            )

            entry = Script2VideoPipeline._build_fixed_registry_entry(
                "林老师", asset, os.path.join(root, "out")
            )

            with open(entry["林老师"]["front"]["path"], encoding="utf-8") as stream:
                self.assertEqual(stream.read(), "selected")

    def test_resolve_fixed_asset_requires_binding_and_type(self):
        with tempfile.TemporaryDirectory() as root:
            asset = self._asset(root)
            reg = CharacterAssetRegistry({"teacher_lin": asset})

            # bound -> resolves
            fake = types.SimpleNamespace(asset_registry=reg, character_bindings={"林老师": "teacher_lin"})
            self.assertIs(Script2VideoPipeline._resolve_fixed_asset(fake, "林老师"), asset)

            # unbound identifier -> None
            self.assertIsNone(Script2VideoPipeline._resolve_fixed_asset(fake, "校长"))

            # no registry -> None
            fake_no_reg = types.SimpleNamespace(asset_registry=None, character_bindings={"林老师": "teacher_lin"})
            self.assertIsNone(Script2VideoPipeline._resolve_fixed_asset(fake_no_reg, "林老师"))

            # bound but non-reference type -> None
            lora = CharacterAsset(asset_id="lin_lora", display_name="林老师", type="lora")
            reg2 = CharacterAssetRegistry({"lin_lora": lora})
            fake2 = types.SimpleNamespace(asset_registry=reg2, character_bindings={"林老师": "lin_lora"})
            self.assertIsNone(Script2VideoPipeline._resolve_fixed_asset(fake2, "林老师"))


class TestIdeaPipelineFixedInjection(unittest.TestCase):
    def test_idea_pipeline_reuses_fixed_asset(self):
        with tempfile.TemporaryDirectory() as root:
            for view in ("front", "side"):
                open(os.path.join(root, f"{view}.png"), "w", encoding="utf-8").write(view)
            asset = CharacterAsset(
                asset_id="teacher_lin",
                display_name="林老师",
                description="年轻女教师。",
                assets={"front": os.path.join(root, "front.png"), "side": os.path.join(root, "side.png")},
            )
            reg = CharacterAssetRegistry({"teacher_lin": asset})
            work = os.path.join(root, "work")
            pipeline = Idea2VideoPipeline(
                chat_model=object(), image_generator=object(), video_generator=object(),
                working_dir=work, character_bindings={"林老师": "teacher_lin"}, asset_registry=reg,
            )
            character = CharacterInScene(idx=0, identifier_in_scene="林老师", is_visible=True, static_features="", dynamic_features="")
            entry = asyncio.run(pipeline.generate_portraits_for_single_character(character, style="国风"))

            self.assertIn("林老师", entry)
            self.assertEqual(set(entry["林老师"].keys()), {"front", "side"})
            self.assertIn("FIXED character asset", entry["林老师"]["front"]["description"])
            self.assertTrue(os.path.exists(entry["林老师"]["front"]["path"]))

    def test_idea_pipeline_unbound_falls_through_to_generation(self):
        # No registry -> resolve returns None -> would proceed to real generation.
        with tempfile.TemporaryDirectory() as root:
            pipeline = Idea2VideoPipeline(
                chat_model=object(), image_generator=object(), video_generator=object(),
                working_dir=os.path.join(root, "work"),
            )
            from characters import resolve_fixed_asset
            self.assertIsNone(resolve_fixed_asset(pipeline.asset_registry, pipeline.character_bindings, "林老师"))


if __name__ == "__main__":
    unittest.main()
