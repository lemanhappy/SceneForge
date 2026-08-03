"""Tests for the on-screen-text constraint: prompting.image_text_constraint()
and the pipeline's generator wrapper that appends it to every image/video prompt
(suppresses spurious foreign/garbled text in generated frames)."""

import asyncio
import unittest

from prompting import (image_text_constraint, DEFAULT_IMAGE_TEXT_CONSTRAINT,
                       IMAGE_TEXT_ESSENTIAL, IMAGE_TEXT_ESSENTIAL_EN, IMAGE_TEXT_NONE,
                       target_language, runtime_language_instruction)
from pipelines.script2video_pipeline import (
    _PromptSuffixGenerator,
    _prepare_frame_references,
    Script2VideoPipeline,
    aspect_to_size,
    size_to_aspect_ratio,
)


class TestTargetLanguage(unittest.TestCase):
    def test_resolution(self):
        self.assertEqual(target_language({"language": {"target_language": "zh-CN"}}), "zh")
        self.assertEqual(target_language({"language": {"target_language": "en"}}), "en")
        self.assertEqual(target_language({"language": {"target_language": ""}}), "")
        self.assertEqual(target_language({}), "")
        self.assertEqual(target_language({"language": {"chinese_mode": True}}), "zh")     # legacy fallback
        # explicit target overrides legacy flag
        self.assertEqual(target_language({"language": {"chinese_mode": True, "target_language": "en"}}), "en")

    def test_runtime_instruction_per_target(self):
        instruction = runtime_language_instruction({"language": {"target_language": "zh-CN"}})
        self.assertIn("简体中文", instruction)
        self.assertIn("visual_desc", instruction)
        self.assertIn("必须使用简体中文", instruction)
        self.assertNotIn("必须用英文", instruction)
        english = runtime_language_instruction({"language": {"target_language": "en"}})
        self.assertIn("English mode", english)
        self.assertIn("must be in natural English", english)
        self.assertEqual(runtime_language_instruction({}), "")

    def test_explicit_follow_script_beats_legacy_chinese_mode(self):
        config = {"language": {"target_language": "", "chinese_mode": True}}
        self.assertEqual(target_language(config), "")
        self.assertEqual(runtime_language_instruction(config), "")

    def test_image_constraint_follows_target_language(self):
        self.assertEqual(image_text_constraint({"language": {"target_language": "zh-CN"}}), IMAGE_TEXT_ESSENTIAL)
        self.assertEqual(image_text_constraint({"language": {"target_language": "en"}}), IMAGE_TEXT_ESSENTIAL_EN)
        # English essential allows English text, not Chinese-only
        self.assertNotIn("Simplified Chinese", IMAGE_TEXT_ESSENTIAL_EN)
        # follow-script (empty) -> no constraint unless a policy is set
        self.assertEqual(image_text_constraint({"language": {"target_language": ""}}), "")


class TestAspectToSize(unittest.TestCase):
    def test_known_aspects(self):
        self.assertEqual(aspect_to_size("landscape"), "1920x1080")
        self.assertEqual(aspect_to_size("portrait"), "1080x1920")
        self.assertEqual(aspect_to_size("square"), "1440x1440")
        self.assertEqual(aspect_to_size("9:16"), "1080x1920")

    def test_case_insensitive_and_default(self):
        self.assertEqual(aspect_to_size("PORTRAIT"), "1080x1920")
        self.assertEqual(aspect_to_size(""), "1920x1080")     # default
        self.assertEqual(aspect_to_size(None), "1920x1080")
        self.assertEqual(aspect_to_size("weird"), "1920x1080")
        # every size keeps both dims within the image model's valid range (≥1024)
        for a in ("landscape", "portrait", "square"):
            w, h = (int(x) for x in aspect_to_size(a).split("x"))
            self.assertGreaterEqual(min(w, h), 1024)

    def test_pipeline_threads_image_size(self):
        p = Script2VideoPipeline(chat_model=object(), image_generator=object(),
                                 video_generator=object(), working_dir="/tmp/x",
                                 image_size="900x1600")
        self.assertEqual(p.image_size, "900x1600")
        self.assertEqual(p.video_aspect_ratio, "9:16")
        self.assertEqual(p.camera_image_generator.image_size, "900x1600")

    def test_size_maps_to_video_provider_aspect(self):
        self.assertEqual(size_to_aspect_ratio("1920x1080"), "16:9")
        self.assertEqual(size_to_aspect_ratio("1080x1920"), "9:16")
        self.assertEqual(size_to_aspect_ratio("1440x1440"), "1:1")


class TestFrameReferencePreparation(unittest.TestCase):
    def test_pins_reusable_assets_and_leads_with_matching_aspect(self):
        import os
        import tempfile
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            portrait = os.path.join(tmp, "portrait.png")
            landscape = os.path.join(tmp, "scene.png")
            prop = os.path.join(tmp, "prop.png")
            Image.new("RGB", (512, 1024)).save(portrait)
            Image.new("RGB", (1600, 900)).save(landscape)
            Image.new("RGB", (800, 800)).save(prop)

            pairs, prompt = _prepare_frame_references(
                [(portrait, "character")],
                [(prop, "[prop] bound prop"), (landscape, "[scene] bound scene")],
                "Use Image 0 for the character.",
                "1920x1080",
            )

            self.assertEqual(pairs[0][0], landscape)
            self.assertEqual({path for path, _ in pairs}, {portrait, landscape, prop})
            self.assertIn("Use Image 1 for the character.", prompt)
            self.assertIn("strict 1920:1080 landscape canvas", prompt)
            self.assertEqual(prompt.count("explicitly bound reusable asset"), 2)
            self.assertIn("canonical appearance-only prop reference", prompt)
            self.assertIn("Never paste it into the foreground", prompt)
            self.assertIn("world-layout reference", prompt)
            self.assertIn("not a camera-composition template", prompt)


class TestConstraintResolution(unittest.TestCase):
    def test_off_when_not_chinese_mode(self):
        self.assertEqual(image_text_constraint({}), "")
        self.assertEqual(image_text_constraint({"language": {"chinese_mode": False}}), "")

    def test_on_when_chinese_mode(self):
        # chinese_mode with no explicit policy -> the nuanced "essential" default
        self.assertEqual(image_text_constraint({"language": {"chinese_mode": True}}),
                         DEFAULT_IMAGE_TEXT_CONSTRAINT)
        self.assertEqual(DEFAULT_IMAGE_TEXT_CONSTRAINT, IMAGE_TEXT_ESSENTIAL)

    def test_policy_values(self):
        self.assertEqual(image_text_constraint({"language": {"image_text_policy": "essential"}}), IMAGE_TEXT_ESSENTIAL)
        self.assertEqual(image_text_constraint({"language": {"image_text_policy": "none"}}), IMAGE_TEXT_NONE)
        self.assertEqual(image_text_constraint({"language": {"image_text_policy": "off"}}), "")
        # essential allows essential in-scene Chinese (mentions 手机/PPT scenarios)
        self.assertIn("essential", IMAGE_TEXT_ESSENTIAL.lower())
        # none forbids all text
        self.assertIn("without any written text", IMAGE_TEXT_NONE.lower())

    def test_explicit_override_wins(self):
        # raw string override beats policy/chinese_mode
        cfg = {"language": {"chinese_mode": True, "image_text_policy": "none", "image_text_constraint": "仅中文文字"}}
        self.assertEqual(image_text_constraint(cfg), "仅中文文字")
        # empty string explicitly disables it
        cfg2 = {"language": {"chinese_mode": True, "image_text_constraint": ""}}
        self.assertEqual(image_text_constraint(cfg2), "")


class _RecordingGen:
    def __init__(self):
        self.image_prompts = []
        self.video_prompts = []
        self.size = "rememberme"

    async def generate_single_image(self, prompt, reference_image_paths=None, **kw):
        self.image_prompts.append(prompt)
        return "img"

    async def generate_single_video(self, prompt, reference_image_paths=None, **kw):
        self.video_prompts.append(prompt)
        return "vid"


class TestWrapper(unittest.TestCase):
    def test_appends_suffix_to_both_prompts(self):
        inner = _RecordingGen()
        w = _PromptSuffixGenerator(inner, "NO-FOREIGN-TEXT")
        asyncio.run(w.generate_single_image("a castle", reference_image_paths=[]))
        asyncio.run(w.generate_single_video("pan left"))
        self.assertEqual(inner.image_prompts[0], "a castle\n\nNO-FOREIGN-TEXT")
        self.assertEqual(inner.video_prompts[0], "pan left\n\nNO-FOREIGN-TEXT")

    def test_empty_prompt_becomes_suffix(self):
        inner = _RecordingGen()
        w = _PromptSuffixGenerator(inner, "S")
        asyncio.run(w.generate_single_image("", reference_image_paths=[]))
        self.assertEqual(inner.image_prompts[0], "S")

    def test_delegates_other_attributes(self):
        inner = _RecordingGen()
        w = _PromptSuffixGenerator(inner, "S")
        self.assertEqual(w.size, "rememberme")  # passthrough via __getattr__


class TestPipelineWiring(unittest.TestCase):
    def test_pipeline_wraps_generators_only_when_constraint_set(self):
        plain = Script2VideoPipeline(chat_model=object(), image_generator=_RecordingGen(),
                                     video_generator=_RecordingGen(), working_dir="/tmp/x")
        self.assertNotIsInstance(plain.image_generator, _PromptSuffixGenerator)

        wrapped = Script2VideoPipeline(chat_model=object(), image_generator=_RecordingGen(),
                                       video_generator=_RecordingGen(), working_dir="/tmp/x",
                                       image_text_constraint="ZH-ONLY")
        self.assertIsInstance(wrapped.image_generator, _PromptSuffixGenerator)
        self.assertIsInstance(wrapped.video_generator, _PromptSuffixGenerator)
        # the constraint actually reaches the inner generator's prompt
        inner = wrapped.image_generator._inner
        asyncio.run(wrapped.image_generator.generate_single_image("scene", reference_image_paths=[]))
        self.assertIn("ZH-ONLY", inner.image_prompts[0])


if __name__ == "__main__":
    unittest.main()
