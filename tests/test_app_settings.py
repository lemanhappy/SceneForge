import asyncio
import tempfile
import unittest
from pathlib import Path

from agent_runtime.session_index import SessionIndex
from server.app_settings_api import AppSettingsAPI
from server.app_settings_service import AppSettingsService


class AppSettingsTests(unittest.TestCase):
    def test_theme_and_media_root_persist_and_apply_to_new_projects(self):
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as media:
            index = SessionIndex(workspace)
            service = AppSettingsService(workspace, index)
            old = index.create(idea="old")
            old_dir = index.working_dir(old["session_id"])

            configured = Path(media) / "SceneForge Media"
            state = service.update({"theme": "dark", "media_root": str(configured)})
            self.assertEqual(state["theme"], "dark")
            self.assertEqual(Path(state["media_root"]), configured.resolve())

            new = index.create(idea="new")
            self.assertEqual(index.working_dir(new["session_id"]).parent, configured.resolve())
            self.assertEqual(index.working_dir(old["session_id"]), old_dir)

            restarted = AppSettingsService(workspace, index)
            self.assertEqual(restarted.get()["theme"], "dark")
            self.assertEqual(index.working_root, configured.resolve())

    def test_relative_media_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as workspace:
            service = AppSettingsService(workspace, SessionIndex(workspace))
            with self.assertRaisesRegex(ValueError, "绝对路径"):
                service.update({"media_root": "relative/folder"})


class AppSettingsApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_put_and_validation(self):
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as media:
            api = AppSettingsAPI(AppSettingsService(workspace, SessionIndex(workspace)))
            status, state = await api.handle("GET", "/api/app-settings")
            self.assertEqual(status, 200)
            self.assertEqual(state["theme"], "light")
            status, state = await api.handle("PUT", "/api/app-settings", {"theme": "dark", "media_root": media})
            self.assertEqual(status, 200)
            self.assertEqual(state["theme"], "dark")
            self.assertEqual((await api.handle("PUT", "/api/app-settings", {"theme": "blue"}))[0], 400)


if __name__ == "__main__":
    unittest.main()
