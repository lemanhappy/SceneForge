import os
import unittest
from unittest.mock import patch

from tools.render_backend import _resolve_api_key


class TestRenderBackendConfig(unittest.TestCase):
    @patch.dict(os.environ, {"SCENEFORGE_IMAGE_API_KEY": "image-key"}, clear=True)
    def test_resolves_key_from_first_available_environment_variable(self):
        result = _resolve_api_key(
            {"model": "image-model", "api_key": ""},
            ("SCENEFORGE_IMAGE_API_KEY", "SCENEFORGE_API_KEY"),
        )
        self.assertEqual(result["api_key"], "image-key")

    @patch.dict(os.environ, {"SCENEFORGE_IMAGE_API_KEY": "environment-key"}, clear=True)
    def test_explicit_key_takes_precedence(self):
        result = _resolve_api_key(
            {"api_key": "explicit-key"},
            ("SCENEFORGE_IMAGE_API_KEY",),
        )
        self.assertEqual(result["api_key"], "explicit-key")

    @patch.dict(os.environ, {}, clear=True)
    def test_keeps_empty_key_when_environment_is_not_configured(self):
        result = _resolve_api_key({"api_key": ""}, ("SCENEFORGE_IMAGE_API_KEY",))
        self.assertEqual(result["api_key"], "")


if __name__ == "__main__":
    unittest.main()
