import asyncio
import tempfile
import unittest
from pathlib import Path

from agent_runtime.session_factory import create_session_index
from infrastructure.sqlite import SQLiteDatabase
from server.production_api import ProductionAPI
from server.series_api import SeriesAPI
from server.series_service import SeriesService


class _FakeProductionService:
    def __init__(self):
        self.kwargs = None

    def start_topic(self, idea, **kwargs):
        self.kwargs = {"idea": idea, **kwargs}
        return {"job_id": "job-series-1"}


class SeriesServiceTests(unittest.TestCase):
    def _services(self, root):
        database = SQLiteDatabase(Path(root) / ".sceneforge" / "sceneforge.db")
        index = create_session_index(root, database_path=database.path, auto_import_legacy=False)
        return index, SeriesService(database, index)

    def test_create_update_and_list_series(self):
        with tempfile.TemporaryDirectory() as root:
            _index, service = self._services(root)
            created = service.create({
                "title": "雨夜归途",
                "premise": "林夏追查父亲失踪的真相",
                "planned_episode_count": 12,
                "episode_duration_sec": 60,
                "aspect_ratio": "portrait",
                "character_asset_ids": ["linxia", "linxia"],
            })
            self.assertEqual(created["episode_count"], 0)
            self.assertEqual(created["next_episode_number"], 1)
            self.assertEqual(created["character_asset_ids"], ["linxia"])

            updated = service.update(created["series_id"], {"planned_episode_count": 10})
            self.assertEqual(updated["planned_episode_count"], 10)
            self.assertEqual(service.list()[0]["series_id"], created["series_id"])

    def test_episode_sequence_and_previous_episode_handoff(self):
        with tempfile.TemporaryDirectory() as root:
            index, service = self._services(root)
            series = service.create({"title": "雨夜归途", "planned_episode_count": 3})
            first = service.prepare_episode(series["series_id"])
            self.assertEqual(first["episode_number"], 1)
            self.assertEqual(first["previous_episode_id"], "")

            first_record = index.create(
                idea="第一集", session_id="rain-ep-1", series_id=series["series_id"],
                episode_number=1, episode_title="失踪的钥匙",
            )
            second = service.prepare_episode(series["series_id"])
            self.assertEqual(second["episode_number"], 2)
            self.assertEqual(second["previous_episode_id"], first_record["session_id"])
            with self.assertRaisesRegex(ValueError, "第 2 集"):
                service.prepare_episode(series["series_id"], 3)


class SeriesApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_crud_and_episode_submission_inherits_series_defaults(self):
        with tempfile.TemporaryDirectory() as root:
            database = SQLiteDatabase(Path(root) / ".sceneforge" / "sceneforge.db")
            index = create_session_index(root, database_path=database.path, auto_import_legacy=False)
            series_service = SeriesService(database, index)
            api = SeriesAPI(series_service)
            status, series = await api.handle("POST", "/api/series", {
                "title": "雨夜归途",
                "premise": "寻找父亲",
                "planned_episode_count": 2,
                "style": "冷蓝雨夜",
                "character_asset_ids": ["linxia"],
            })
            self.assertEqual(status, 201)
            self.assertEqual((await api.handle("GET", "/api/series"))[0], 200)

            production = _FakeProductionService()
            production_api = ProductionAPI(index, production, adapters=None, series_service=series_service)
            status, result = await production_api.handle("POST", "/api/production/topic", {
                "series_id": series["series_id"],
                "episode_number": 1,
                "episode_title": "失踪的钥匙",
                "idea": "林夏在雨夜发现钥匙",
            })
            self.assertEqual(status, 200)
            self.assertEqual(result["job_id"], "job-series-1")
            self.assertEqual(production.kwargs["series_id"], series["series_id"])
            self.assertEqual(production.kwargs["episode_number"], 1)
            self.assertEqual(production.kwargs["character_asset_ids"], ["linxia"])
            self.assertEqual(production.kwargs["style"], "冷蓝雨夜")


if __name__ == "__main__":
    unittest.main()
