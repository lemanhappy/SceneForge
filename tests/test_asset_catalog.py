import tempfile
from pathlib import Path

from characters import (
    AssetCatalog,
    CharacterAsset,
    IdentityProfile,
    LoRABinding,
    OutfitVersion,
    ProviderCharacterBinding,
    ReferenceSet,
    ReusableAsset,
)
from infrastructure.sqlite import SQLiteAssetCatalogRepository, SQLiteDatabase
from agent_runtime.session_index import SessionIndex
from services.workflow_engine import WorkflowEngine


def _image(path: Path) -> str:
    path.write_bytes(b"reference")
    return str(path)


def test_character_keeps_references_and_optional_lora_side_by_side():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        front = _image(root / "front.png")
        side = _image(root / "side.png")
        crying = _image(root / "cry.png")
        asset = CharacterAsset(
            asset_id="lead",
            display_name="主角",
            identity_profile=IdentityProfile(
                facial_features="oval face",
                hairstyle="long black hair",
                forbidden_changes=["eye color", "hairstyle"],
            ),
            reference_sets=[ReferenceSet(
                reference_set_id="costume_a",
                images={"front": front, "side": side},
                expressions={"cry": crying},
                outfit_version_id="costume_a",
                is_default=True,
            )],
            outfit_versions=[OutfitVersion(
                outfit_version_id="costume_a",
                name="古装",
                is_default=True,
            )],
            render_bindings=[
                LoRABinding(
                    binding_id="lead_lora",
                    model_path="models/lead.safetensors",
                    trigger_words=["lead_person"],
                ),
                ProviderCharacterBinding(
                    binding_id="cloud_character",
                    provider="example",
                    character_id="char_123",
                    enabled=True,
                ),
            ],
        )

        repository = SQLiteAssetCatalogRepository(SQLiteDatabase(root / "catalog.db"))
        repository.upsert_character(asset)
        loaded = repository.get_character("lead")

        assert loaded is not None
        assert loaded.default_reference_set().images["front"] == front
        assert loaded.render_bindings[0].kind == "lora"
        assert loaded.render_bindings[0].enabled is False
        assert [item.kind for item in loaded.enabled_render_bindings()] == ["provider_character_id"]
        assert "Never change" in loaded.identity_constraint()

        with repository.database.connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM reference_sets").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM outfit_versions").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM render_bindings").fetchone()[0] == 2


def test_catalog_selects_view_and_expression_for_the_shot():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        front = _image(root / "front.png")
        side = _image(root / "side.png")
        crying = _image(root / "cry.png")
        asset = CharacterAsset(
            asset_id="lead",
            display_name="Lead",
            reference_sets=[ReferenceSet(
                reference_set_id="default",
                images={"front": front, "side": side},
                expressions={"cry": crying},
                is_default=True,
            )],
        )
        catalog = AssetCatalog({"lead": asset})

        side_selection = catalog.select_references("lead", "人物侧脸看向窗外")
        crying_selection = catalog.select_references("lead", "眼泪滑落，克制地哭")

        assert [(item.view, item.path) for item in side_selection] == [("side", side)]
        assert [(item.view, item.path) for item in crying_selection] == [("expression_cry", crying)]


def test_catalog_imports_legacy_yaml_once_and_remains_portable():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _image(root / "front.png")
        registry = root / "registry.yaml"
        registry.write_text(
            "characters:\n"
            "  lead:\n"
            "    display_name: Lead\n"
            "    type: reference_images\n"
            "    assets:\n"
            "      front: front.png\n",
            encoding="utf-8",
        )
        repository = SQLiteAssetCatalogRepository(SQLiteDatabase(root / "catalog.db"))

        catalog = AssetCatalog.open_catalog(str(registry), repository)

        assert catalog.get("lead") is not None
        assert repository.get_character("lead") is not None
        assert repository.get_character("lead").default_reference_set().images["front"] == str(root / "front.png")


def test_repository_keeps_prop_and_scene_models_separate():
    with tempfile.TemporaryDirectory() as tmp:
        repository = SQLiteAssetCatalogRepository(SQLiteDatabase(Path(tmp) / "catalog.db"))
        prop = ReusableAsset(
            asset_id="hero_sword",
            asset_type="prop",
            display_name="玄铁长剑",
            visual_prompt="black iron blade with a silver crack",
            negative_prompt="never change the grip color",
            tags=["古装", "主道具"],
        )
        scene = ReusableAsset(
            asset_id="wangfu_yard",
            asset_type="scene",
            display_name="王府后院",
            visual_prompt="grey brick courtyard with one old plum tree",
        )

        repository.upsert_asset(prop)
        repository.upsert_asset(scene)

        assert [item.asset_id for item in repository.list_assets("prop")] == ["hero_sword"]
        assert [item.asset_id for item in repository.list_assets("scene")] == ["wangfu_yard"]
        assert "禁止变化" in repository.get_asset("hero_sword").prompt_constraint()
        assert repository.get_character("hero_sword") is None
        assert repository.remove_asset("hero_sword") is True
        assert repository.get_asset("hero_sword") is None


def test_workflow_injects_selected_asset_constraints_and_reference_images():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        reference = _image(root / "sword.png")
        repository = SQLiteAssetCatalogRepository(SQLiteDatabase(root / "catalog.db"))
        repository.upsert_asset(ReusableAsset(
            asset_id="hero_sword",
            asset_type="prop",
            display_name="Hero sword",
            visual_prompt="black iron blade with a silver crack",
            negative_prompt="never change the grip color",
            assets={"reference": reference},
        ))
        engine = WorkflowEngine(
            SessionIndex(root),
            root,
            asset_catalog_repository=repository,
        )
        session = {
            "user_requirement": "restrained farewell",
            "prop_asset_ids": ["hero_sword"],
            "scene_asset_ids": [],
        }

        requirement = engine._augment_requirement(session, "")
        assert "black iron blade with a silver crack" in requirement
        assert "never change the grip color" in requirement
        assert engine._reusable_reference_pairs(session) == [
            (reference, f"[prop] {repository.get_asset('hero_sword').prompt_constraint()}")
        ]


def test_character_cannot_overwrite_a_prop_with_the_same_asset_id():
    with tempfile.TemporaryDirectory() as tmp:
        repository = SQLiteAssetCatalogRepository(SQLiteDatabase(Path(tmp) / "catalog.db"))
        repository.upsert_asset(ReusableAsset(
            asset_id="shared_id",
            asset_type="prop",
            display_name="Shared prop",
        ))

        try:
            repository.upsert_character(CharacterAsset(
                asset_id="shared_id",
                display_name="Shared character",
            ))
        except ValueError as exc:
            assert "already belongs to prop" in str(exc)
        else:
            raise AssertionError("character model silently replaced a prop model")
