from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .asset_registry import CharacterAssetRegistry
from .models import CharacterAsset, ReferenceSet, RenderBinding


@dataclass(frozen=True, slots=True)
class SelectedReference:
    asset_id: str
    reference_set_id: str
    view: str
    path: str
    outfit_version_id: str | None = None


class AssetCatalog(CharacterAssetRegistry):
    """Unified character catalog with a legacy registry-compatible surface."""

    def __init__(self, *args, repository: Any = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.repository = repository

    @classmethod
    def from_registry(
        cls, registry: CharacterAssetRegistry, repository: Any = None
    ) -> "AssetCatalog":
        return cls(
            {asset.asset_id: asset for asset in registry.all()},
            base_dir=registry.base_dir,
            registry_path=registry.registry_path,
            repository=repository,
        )

    @classmethod
    def open_catalog(cls, path: str, repository: Any = None) -> "AssetCatalog":
        registry = CharacterAssetRegistry.open_or_create(path)
        if repository is not None:
            stored = repository.list_characters()
            stored_ids = {asset.asset_id for asset in stored}
            for asset in registry.all():
                if asset.asset_id not in stored_ids:
                    repository.upsert_character(asset)
            for asset in stored:
                registry.upsert(asset)
        return cls.from_registry(registry, repository=repository)

    def upsert(self, asset: CharacterAsset) -> None:
        super().upsert(asset)
        if self.repository is not None:
            self.repository.upsert_character(asset)

    def remove(self, asset_id: str) -> bool:
        removed = super().remove(asset_id)
        if self.repository is not None:
            self.repository.remove_character(asset_id)
        return removed

    def select_references(
        self,
        asset_id: str,
        frame_description: str,
        *,
        outfit_version_id: str | None = None,
        max_references: int = 1,
    ) -> list[SelectedReference]:
        asset = self.get(asset_id)
        if asset is None or max_references <= 0:
            return []
        reference_set = asset.default_reference_set(outfit_version_id)
        if reference_set is None:
            return []
        view_order = _preferred_views(frame_description)
        available = reference_set.all_images()
        selected: list[SelectedReference] = []
        for view in view_order:
            path = available.get(view)
            if path and Path(path).is_file():
                selected.append(_selection(asset, reference_set, view, path))
                break
        if not selected:
            for view, path in available.items():
                if path and Path(path).is_file():
                    selected.append(_selection(asset, reference_set, view, path))
                    break

        # A second complementary full-body/front reference is useful for complex
        # poses, while the default remains one image per visible character.
        if max_references > 1 and selected:
            for view in ("full_body", "front"):
                path = available.get(view)
                if path and Path(path).is_file() and path != selected[0].path:
                    selected.append(_selection(asset, reference_set, view, path))
                    break
        return selected[:max_references]

    def render_bindings(
        self, asset_id: str, *, enabled_only: bool = True, kind: str | None = None
    ) -> list[RenderBinding]:
        asset = self.get(asset_id)
        if asset is None:
            return []
        bindings: Iterable[RenderBinding] = asset.render_bindings
        return [
            binding for binding in bindings
            if (not enabled_only or binding.enabled)
            and (kind is None or binding.kind == kind)
        ]


def _selection(
    asset: CharacterAsset, reference_set: ReferenceSet, view: str, path: str
) -> SelectedReference:
    return SelectedReference(
        asset_id=asset.asset_id,
        reference_set_id=reference_set.reference_set_id,
        view=view,
        path=path,
        outfit_version_id=reference_set.outfit_version_id,
    )


def _preferred_views(description: str) -> tuple[str, ...]:
    text = str(description or "").lower()
    if re.search(r"\b(back|rear)\b|背面|背对|后背", text):
        return ("back", "side", "front", "full_body")
    if re.search(r"\b(side|profile)\b|侧面|侧脸|侧身", text):
        return ("side", "front", "full_body", "back")
    if re.search(r"\bfull[- ]?body\b|全身|远景", text):
        return ("full_body", "front", "side", "back")
    if re.search(r"smil|笑", text):
        return ("expression_smile", "front", "side", "full_body")
    if re.search(r"cry|tear|哭|泪", text):
        return ("expression_cry", "front", "side", "full_body")
    return ("front", "side", "full_body", "back")
