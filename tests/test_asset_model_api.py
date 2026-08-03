import asyncio
import tempfile
from pathlib import Path

from characters.library_studio import AssetModelStudio
from infrastructure.sqlite import SQLiteAssetCatalogRepository, SQLiteDatabase
from server.asset_api import AssetModelAPI


class _Image:
    def save(self, path):
        Path(path).write_bytes(b"image")


class _Generator:
    def __init__(self):
        self.prompts = []

    async def generate_single_image(self, prompt, reference_image_paths):
        self.prompts.append((prompt, reference_image_paths))
        return _Image()


def test_asset_model_crud_and_reference_generation():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        generator = _Generator()
        repository = SQLiteAssetCatalogRepository(SQLiteDatabase(root / "catalog.db"))
        api = AssetModelAPI(AssetModelStudio(repository, generator, root / "models"))

        status, created = asyncio.run(api.handle("POST", "/api/assets", {
            "asset_id": "hero_sword",
            "asset_type": "prop",
            "display_name": "玄铁长剑",
            "visual_prompt": "black iron blade",
            "negative_prompt": "no logo",
            "prop_bible": {
                "materials": ["black iron"],
                "ownership": "the lead",
                "initial_location": "wooden table",
            },
        }))
        assert status == 200
        assert created["asset_type"] == "prop"
        assert created["prop_bible"]["ownership"] == "the lead"

        status, body = asyncio.run(api.handle("GET", "/api/assets?asset_type=prop"))
        assert status == 200
        assert [item["asset_id"] for item in body["assets"]] == ["hero_sword"]
        assert body["assets"][0]["prop_bible"]["initial_location"] == "wooden table"

        status, generated = asyncio.run(api.handle(
            "POST", "/api/assets/hero_sword/generate", {}))
        assert status == 200
        assert Path(generated["path"]).is_file()
        assert "no logo" in generator.prompts[0][0]
        assert generator.prompts[0][1] == []

        status, image = asyncio.run(api.handle("GET", "/api/assets/hero_sword/image"))
        assert status == 200
        assert Path(image["_file"]).is_file()

        status, removed = asyncio.run(api.handle("DELETE", "/api/assets/hero_sword"))
        assert status == 200
        assert removed == {"removed": True}
        assert not Path(generated["path"]).exists()
        assert not Path(generated["path"]).parent.exists()


def test_asset_model_rejects_invalid_kind_and_duplicate():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        api = AssetModelAPI(AssetModelStudio(
            SQLiteAssetCatalogRepository(SQLiteDatabase(root / "catalog.db")),
            _Generator(),
            root / "models",
        ))
        status, _ = asyncio.run(api.handle("POST", "/api/assets", {
            "asset_id": "one", "asset_type": "audio", "display_name": "x",
        }))
        assert status == 400
        asyncio.run(api.handle("POST", "/api/assets", {
            "asset_id": "one", "asset_type": "scene", "display_name": "x",
        }))
        status, body = asyncio.run(api.handle("POST", "/api/assets", {
            "asset_id": "one", "asset_type": "scene", "display_name": "x",
        }))
        assert status == 409
        assert body["exists"] is True
