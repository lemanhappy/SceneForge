"""Tests for asset_id format validation + create-time uniqueness (no silent
overwrite on the create route)."""

import asyncio
import os
import tempfile
import unittest

from characters.studio import CharacterStudio
from server.character_api import CharacterStudioAPI, _validate_asset_id, _ASSET_ID_RE


def _run(coro):
    return asyncio.run(coro)


class TestValidate(unittest.TestCase):
    def test_valid_slugs(self):
        for good in ("wangyunbao", "wyb", "char-1", "Char_2", "a"):
            self.assertEqual(_validate_asset_id(good), good)

    def test_rejects_bad(self):
        for bad in ("", "  ", "../x", "a/b", "王云宝", "has space", "-leading", "_leading", "name.png"):
            with self.assertRaises(ValueError):
                _validate_asset_id(bad)

    def test_regex_anchors(self):
        self.assertTrue(_ASSET_ID_RE.match("ok-1"))
        self.assertFalse(_ASSET_ID_RE.match("bad/x"))


class TestApi(unittest.TestCase):
    def _api(self, d):
        return CharacterStudioAPI(CharacterStudio(os.path.join(d, "registry.yaml"), image_generator=None))

    def test_create_then_duplicate_conflicts(self):
        with tempfile.TemporaryDirectory() as d:
            api = self._api(d)
            st, _ = _run(api.handle("POST", "/api/characters", {"asset_id": "wyb", "display_name": "王云宝"}))
            self.assertEqual(st, 200)
            # second create with same id -> 409, not a silent overwrite
            st, body = _run(api.handle("POST", "/api/characters", {"asset_id": "wyb", "display_name": "别人"}))
            self.assertEqual(st, 409)
            self.assertTrue(body.get("exists"))
            # original untouched
            _, got = _run(api.handle("GET", "/api/characters/wyb"))
            self.assertEqual(got["display_name"], "王云宝")

    def test_overwrite_flag_allows_update(self):
        with tempfile.TemporaryDirectory() as d:
            api = self._api(d)
            _run(api.handle("POST", "/api/characters", {"asset_id": "wyb", "display_name": "王云宝"}))
            st, _ = _run(api.handle("POST", "/api/characters",
                                    {"asset_id": "wyb", "display_name": "新名", "overwrite": True}))
            self.assertEqual(st, 200)
            _, got = _run(api.handle("GET", "/api/characters/wyb"))
            self.assertEqual(got["display_name"], "新名")

    def test_edit_route_still_upserts(self):
        with tempfile.TemporaryDirectory() as d:
            api = self._api(d)
            _run(api.handle("POST", "/api/characters", {"asset_id": "wyb", "display_name": "王云宝"}))
            # the explicit per-id route is an edit -> upsert allowed without flag
            st, _ = _run(api.handle("POST", "/api/characters/wyb", {"display_name": "改了"}))
            self.assertEqual(st, 200)
            _, got = _run(api.handle("GET", "/api/characters/wyb"))
            self.assertEqual(got["display_name"], "改了")

    def test_invalid_id_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            api = self._api(d)
            for bad in ("王云宝", "../escape", "a b", ""):
                st, _ = _run(api.handle("POST", "/api/characters", {"asset_id": bad}))
                self.assertEqual(st, 400)


if __name__ == "__main__":
    unittest.main()
