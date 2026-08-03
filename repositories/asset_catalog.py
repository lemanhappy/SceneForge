from __future__ import annotations

from typing import Protocol, runtime_checkable

from characters.models import CharacterAsset


@runtime_checkable
class AssetCatalogRepository(Protocol):
    def upsert_character(
        self,
        asset: CharacterAsset,
        *,
        scope: str = "global",
        project_id: str | None = None,
    ) -> CharacterAsset: ...

    def get_character(self, asset_id: str) -> CharacterAsset | None: ...

    def list_characters(self, project_id: str | None = None) -> list[CharacterAsset]: ...

    def remove_character(self, asset_id: str) -> bool: ...
