from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from .models import ReusableAsset


class AssetModelStudio:
    """Create and render reusable prop/environment reference models."""

    def __init__(self, repository: Any, image_generator: Any, assets_root: str | Path):
        self.repository = repository
        self.image_generator = image_generator
        self.assets_root = Path(assets_root)

    def list_assets(self, asset_type: str | None = None) -> list[dict]:
        return [self._public(item) for item in self.repository.list_assets(asset_type)]

    def get(self, asset_id: str) -> dict | None:
        item = self.repository.get_asset(asset_id)
        return self._public(item) if item else None

    def upsert(self, asset_id: str, payload: dict) -> dict:
        existing = self.repository.get_asset(asset_id)
        asset_type = str(payload.get("asset_type") or (existing.asset_type if existing else ""))
        if asset_type not in {"prop", "scene"}:
            raise ValueError("asset_type must be prop or scene")
        item = ReusableAsset(
            asset_id=asset_id,
            asset_type=asset_type,
            display_name=str(payload.get("display_name") or (existing.display_name if existing else asset_id)),
            aliases=_string_list(payload.get("aliases"), existing.aliases if existing else []),
            description=_value(payload, "description", existing.description if existing else ""),
            visual_prompt=_value(payload, "visual_prompt", existing.visual_prompt if existing else ""),
            negative_prompt=_value(payload, "negative_prompt", existing.negative_prompt if existing else ""),
            consistency_notes=_value(payload, "consistency_notes", existing.consistency_notes if existing else ""),
            tags=_string_list(payload.get("tags"), existing.tags if existing else []),
            assets=dict(existing.assets) if existing else {},
            scene_bible=(payload.get("scene_bible") if "scene_bible" in payload
                         else (existing.scene_bible if existing else None)),
            prop_bible=(payload.get("prop_bible") if "prop_bible" in payload
                        else (existing.prop_bible if existing else None)),
        )
        self.repository.upsert_asset(item)
        return self._public(item)

    def remove(self, asset_id: str) -> bool:
        item = self.repository.get_asset(asset_id)
        if item is None:
            return False
        folder = self._asset_folder(item)
        if not self.repository.remove_asset(asset_id):
            return False
        if folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True)
        return True

    def image_path(self, asset_id: str, view: str = "reference") -> str | None:
        item = self.repository.get_asset(asset_id)
        if item is None:
            return None
        path = item.assets.get(view)
        return path if path and os.path.isfile(path) else None

    async def generate_reference(self, asset_id: str, extra_prompt: str = "") -> dict:
        item = self.repository.get_asset(asset_id)
        if item is None:
            raise KeyError(asset_id)
        core = item.visual_prompt.strip() or item.description.strip()
        if item.asset_type == "prop":
            prompt = (
                f"Canonical product reference image of {item.display_name}. {core}. {extra_prompt}. "
                "Show the complete object clearly, stable shape, materials, colors and distinctive details, "
                "neutral studio background, no people, no text, no duplicate objects."
            )
        else:
            prompt = (
                f"Canonical environment reference image of {item.display_name}. {core}. {extra_prompt}. "
                "Wide establishing view, stable architecture, layout, lighting anchors and color palette, "
                "no people, no text, suitable as a consistent scene reference."
            )
        if item.negative_prompt.strip():
            prompt += f" Avoid: {item.negative_prompt.strip()}."
        image = await self.image_generator.generate_single_image(
            prompt=prompt.strip(), reference_image_paths=[]
        )
        folder = self._asset_folder(item)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "reference.png"
        image.save(str(target))
        item.assets["reference"] = str(target)
        self.repository.upsert_asset(item)
        return {"asset_id": item.asset_id, "asset_type": item.asset_type, "path": str(target)}

    @staticmethod
    def _public(item: ReusableAsset) -> dict:
        return item.model_dump(mode="json")

    def _asset_folder(self, item: ReusableAsset) -> Path:
        root = self.assets_root.resolve()
        folder = (root / item.asset_type / item.asset_id).resolve()
        if root not in folder.parents:
            raise ValueError("asset path escapes the configured models directory")
        return folder


def _value(payload: dict, key: str, fallback: str) -> str:
    return str(payload[key] or "") if key in payload else str(fallback or "")


def _string_list(value, fallback) -> list[str]:
    if value is None:
        return list(fallback or [])
    if not isinstance(value, list):
        raise ValueError("aliases and tags must be lists")
    return [str(item).strip() for item in value if str(item).strip()]
