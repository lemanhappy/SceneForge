"""The web flow's image generator must be provider-aware: a seedream model (or an
explicit provider override) selects the Doubao Seedream backend; otherwise the
nano-banana (gemini-image) backend. Driven by config so users can switch the image
model from the settings page without code changes.

Accessors are patched directly (rather than via env) so the test is independent of
whatever is in the local agent.local.yaml."""

import unittest
from unittest import mock

from agent_runtime.sceneforge_adapters import _build_image_generator
from tools.image_generator_doubao_seedream_yunwu_api import ImageGeneratorDoubaoSeedreamYunwuAPI
from tools.image_generator_nanobanana_yunwu_api import ImageGeneratorNanobananaYunwuAPI

A = "agent_runtime.sceneforge_adapters."


def _patch(model="gemini-2.5-flash-image", provider="", key="test-key", base_url="https://yunwu.ai"):
    return [
        mock.patch(A + "image_api_key", return_value=key),
        mock.patch(A + "image_model", return_value=model),
        mock.patch(A + "image_provider", return_value=provider),
        mock.patch(A + "image_base_url", return_value=base_url),
    ]


class TestImageBuilderProvider(unittest.TestCase):
    def _build(self, **kw):
        ps = _patch(**kw)
        for p in ps:
            p.start()
        try:
            return _build_image_generator()
        finally:
            for p in ps:
                p.stop()

    def test_seedream_selected_by_model_name(self):
        generator = self._build(
            model="doubao-seedream-4-0-250828",
            base_url="https://images.example/v1",
        )
        self.assertIsInstance(generator, ImageGeneratorDoubaoSeedreamYunwuAPI)
        self.assertEqual(generator.base_url, "https://images.example/v1/images/generations")

    def test_nanobanana_is_default(self):
        self.assertIsInstance(self._build(model="gemini-2.5-flash-image", provider=""),
                              ImageGeneratorNanobananaYunwuAPI)

    def test_explicit_provider_override_wins(self):
        # non-seedream model name, but provider forces seedream
        self.assertIsInstance(self._build(model="whatever-model", provider="seedream"),
                              ImageGeneratorDoubaoSeedreamYunwuAPI)

    def test_missing_key_raises(self):
        with self.assertRaises(RuntimeError):
            self._build(key="")


if __name__ == "__main__":
    unittest.main()
