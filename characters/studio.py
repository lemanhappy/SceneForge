"""Character studio: create/refine fixed characters and generate their portraits.

This is the backend for the "角色生成页面": build a character from a description,
generate a portrait, tweak the prompt and regenerate until satisfied, then it is
already saved as a fixed CharacterAsset in the registry — directly reusable by
the video pipeline (固定角色注入) for consistent characters across shots.
"""

import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from .catalog import AssetCatalog
from .models import CharacterAsset, CharacterBible, IdentityProfile, OutfitVersion, ReferenceSet, RenderBinding


class CharacterStudio:
    def __init__(self, registry_path: str, image_generator: Any, assets_root: Optional[str] = None,
                 catalog_repository: Any = None):
        self.registry = AssetCatalog.open_catalog(registry_path, catalog_repository)
        self.image_generator = image_generator
        self.assets_root = Path(assets_root) if assets_root else Path(registry_path).parent

    # ----- read --------------------------------------------------------

    def list_characters(self) -> List[dict]:
        return [self._public(a) for a in self.registry.all()]

    def get(self, asset_id: str) -> Optional[dict]:
        asset = self.registry.get(asset_id)
        return self._public(asset) if asset else None

    def image_path(self, asset_id: str, view: str) -> Optional[str]:
        asset = self.registry.get(asset_id)
        if asset is None:
            return None
        path = (asset.assets or {}).get(view)
        return path if path and os.path.exists(path) else None

    # ----- write -------------------------------------------------------

    def upsert(self, asset_id: str, display_name: Optional[str] = None, description: str = "",
               aliases: Optional[List[str]] = None, visual_prompt: Optional[str] = None,
               identity_profile: Optional[dict] = None,
               bible: Optional[dict] = None,
               reference_sets: Optional[List[dict]] = None,
               outfit_versions: Optional[List[dict]] = None,
               render_bindings: Optional[List[dict]] = None) -> dict:
        if not asset_id or not str(asset_id).strip():
            raise ValueError("asset_id is required")
        existing = self.registry.get(asset_id)
        asset = CharacterAsset(
            asset_id=asset_id,
            display_name=display_name or (existing.display_name if existing else asset_id),
            aliases=aliases if aliases is not None else (existing.aliases if existing else []),
            type="reference_images",
            description=description if description else (existing.description if existing else ""),
            visual_prompt=visual_prompt if visual_prompt is not None else (existing.visual_prompt if existing else None),
            assets=dict(existing.assets) if existing else {},
            provider=existing.provider if existing else None,
            model_id=existing.model_id if existing else None,
            trigger_words=list(existing.trigger_words) if existing else [],
            identity_profile=(IdentityProfile.model_validate(identity_profile)
                              if identity_profile is not None
                              else (existing.identity_profile if existing else IdentityProfile())),
            bible=(CharacterBible.model_validate(bible)
                   if bible is not None
                   else (existing.bible if existing else CharacterBible())),
            reference_sets=([ReferenceSet.model_validate(item) for item in reference_sets]
                            if reference_sets is not None
                            else (list(existing.reference_sets) if existing else [])),
            outfit_versions=([OutfitVersion.model_validate(item) for item in outfit_versions]
                             if outfit_versions is not None
                             else (list(existing.outfit_versions) if existing else [])),
            render_bindings=([_validate_render_binding(item) for item in render_bindings]
                             if render_bindings is not None
                             else (list(existing.render_bindings) if existing else [])),
        )
        self.registry.upsert(asset)
        self.registry.save()
        return self._public(asset)

    def remove(self, asset_id: str) -> bool:
        removed = self.registry.remove(asset_id)
        if removed:
            self.registry.save()
        return removed

    async def generate_view(self, asset_id: str, view: str = "front", style: str = "", extra_prompt: str = "") -> dict:
        """Generate one portrait view. front: from the character's description;
        side/back/expression: edited from the existing front for identity consistency.
        Re-call with a tweaked description/extra_prompt to iterate ("优化")."""
        asset = self.registry.get(asset_id)
        if asset is None:
            raise KeyError(asset_id)

        char_dir = self.assets_root / asset_id
        char_dir.mkdir(parents=True, exist_ok=True)
        base_desc = (asset.visual_prompt or asset.description or "").strip()

        if view == "front":
            prompt = (
                f"Full-body front-view portrait of {asset.display_name}. {base_desc}. {extra_prompt} "
                f"Style: {style}. Centered, occupying most of the frame, gazing ahead, natural expression, "
                f"plain white background."
            ).strip()
            reference_image_paths: List[str] = []
        else:
            front = (asset.assets or {}).get("front")
            if not front or not os.path.exists(front):
                raise ValueError("generate the 'front' view before other views")
            prompt = (
                f"Full-body {view}-view portrait of {asset.display_name} based on the provided front view. "
                f"Keep the character's identity, face, hairstyle and clothing consistent. {extra_prompt} "
                f"Plain white background."
            ).strip()
            reference_image_paths = [front]

        image = await self.image_generator.generate_single_image(prompt=prompt, reference_image_paths=reference_image_paths)
        dst = char_dir / f"{view}.png"
        self._archive_current(char_dir, view)  # keep the previous portrait, versioned
        image.save(str(dst))

        asset.assets[view] = str(dst)
        self._sync_reference_view(asset, view, str(dst))
        self.registry.upsert(asset)
        self.registry.save()
        return {"asset_id": asset_id, "view": view, "path": str(dst), "prompt": prompt}

    # ----- portrait version history -----------------------------------

    def _versions_dir(self, char_dir: Path, view: str) -> Path:
        return char_dir / "_versions" / view

    def _archive_current(self, char_dir: Path, view: str) -> Optional[str]:
        """Copy the current {view}.png into _versions/{view}/v<n>.png before it's
        overwritten, so regenerations are never destructive."""
        cur = char_dir / f"{view}.png"
        if not cur.exists():
            return None
        vdir = self._versions_dir(char_dir, view)
        vdir.mkdir(parents=True, exist_ok=True)
        nums = [int(m.group(1)) for p in vdir.glob("v*.png") for m in [re.match(r"v(\d+)\.png$", p.name)] if m]
        nxt = (max(nums) + 1) if nums else 1
        archived = vdir / f"v{nxt}.png"
        try:
            shutil.copy2(cur, archived)
            return str(archived)
        except OSError:
            return None

    def list_versions(self, asset_id: str, view: str = "front") -> List[dict]:
        """Archived prior versions of a view, newest first."""
        char_dir = self.assets_root / asset_id
        vdir = self._versions_dir(char_dir, view)
        if not vdir.is_dir():
            return []
        items = []
        for p in vdir.glob("v*.png"):
            m = re.match(r"v(\d+)\.png$", p.name)
            if m:
                items.append({"version": int(m.group(1)), "path": str(p)})
        return sorted(items, key=lambda x: x["version"], reverse=True)

    def version_path(self, asset_id: str, view: str, version: int) -> Optional[str]:
        p = self._versions_dir(self.assets_root / asset_id, view) / f"v{int(version)}.png"
        return str(p) if p.exists() else None

    def rollback(self, asset_id: str, view: str, version: int) -> dict:
        """Restore an archived version as the current portrait (archiving the
        current one first, so rollback is itself reversible)."""
        asset = self.registry.get(asset_id)
        if asset is None:
            raise KeyError(asset_id)
        char_dir = self.assets_root / asset_id
        src = self._versions_dir(char_dir, view) / f"v{int(version)}.png"
        if not src.exists():
            raise ValueError(f"version v{version} not found for {view}")
        dst = char_dir / f"{view}.png"
        self._archive_current(char_dir, view)
        shutil.copy2(src, dst)
        asset.assets[view] = str(dst)
        self._sync_reference_view(asset, view, str(dst))
        self.registry.upsert(asset)
        self.registry.save()
        return {"asset_id": asset_id, "view": view, "restored_from": int(version), "path": str(dst)}

    # ----- helpers -----------------------------------------------------

    def _public(self, asset: CharacterAsset) -> dict:
        return {
            "asset_id": asset.asset_id,
            "display_name": asset.display_name,
            "aliases": list(asset.aliases),
            "type": asset.type,
            "description": asset.description,
            "visual_prompt": asset.visual_prompt,
            "views": dict(asset.assets or {}),
            "identity_profile": asset.identity_profile.model_dump(mode="json"),
            "bible": asset.bible.model_dump(mode="json"),
            "reference_sets": [item.model_dump(mode="json") for item in asset.reference_sets],
            "outfit_versions": [item.model_dump(mode="json") for item in asset.outfit_versions],
            "render_bindings": [item.model_dump(mode="json") for item in asset.render_bindings],
            "enabled_render_bindings": [item.model_dump(mode="json") for item in asset.enabled_render_bindings()],
        }

    @staticmethod
    def _sync_reference_view(asset: CharacterAsset, view: str, path: str) -> None:
        reference_set = asset.default_reference_set()
        if reference_set is None:
            reference_set = ReferenceSet(reference_set_id="default", is_default=True)
            asset.reference_sets.append(reference_set)
        reference_set.images[view] = path


def _validate_render_binding(payload: dict) -> RenderBinding:
    kind = str((payload or {}).get("kind") or "")
    from .models import LoRABinding, ProviderCharacterBinding, ThreeDModelBinding
    model = {
        "lora": LoRABinding,
        "provider_character_id": ProviderCharacterBinding,
        "three_d_model": ThreeDModelBinding,
    }.get(kind)
    if model is None:
        raise ValueError(f"unsupported render binding kind: {kind or '<empty>'}")
    return model.model_validate(payload)
