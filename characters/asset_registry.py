import os
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from .models import CharacterAsset


class CharacterAssetRegistry:
    """Loads fixed character assets and matches scene characters to them.

    Image paths in the registry yaml may be absolute or relative; relative paths
    are resolved against ``base_dir`` (the directory containing the registry
    file) so an asset library is self-contained and relocatable.
    """

    def __init__(self, assets: Dict[str, CharacterAsset], base_dir: Optional[Path] = None,
                 registry_path: Optional[Path] = None):
        self._assets: Dict[str, CharacterAsset] = dict(assets)
        self.base_dir = Path(base_dir) if base_dir is not None else None
        self.registry_path = Path(registry_path) if registry_path is not None else None

    # ----- construction -------------------------------------------------

    @classmethod
    def from_yaml(cls, path: str) -> "CharacterAssetRegistry":
        registry_path = Path(path)
        with open(registry_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        base_dir = registry_path.parent
        characters = raw.get("characters", raw) or {}
        assets: Dict[str, CharacterAsset] = {}
        for asset_id, payload in characters.items():
            payload = dict(payload or {})
            payload.setdefault("asset_id", asset_id)
            payload.setdefault("display_name", asset_id)
            payload["assets"] = {
                view: cls._resolve_path(base_dir, p)
                for view, p in (payload.get("assets") or {}).items()
            }
            for reference_set in payload.get("reference_sets") or []:
                reference_set["images"] = {
                    view: cls._resolve_path(base_dir, image_path)
                    for view, image_path in (reference_set.get("images") or {}).items()
                }
                reference_set["expressions"] = {
                    expression: cls._resolve_path(base_dir, image_path)
                    for expression, image_path in (reference_set.get("expressions") or {}).items()
                }
            assets[asset_id] = CharacterAsset.model_validate(payload)
        return cls(assets, base_dir=base_dir, registry_path=registry_path)

    @classmethod
    def open_or_create(cls, path: str) -> "CharacterAssetRegistry":
        """Load an existing registry, or start an empty one rooted at ``path``
        (the file is created on first save). Used by the character studio."""
        registry_path = Path(path)
        if registry_path.exists():
            return cls.from_yaml(str(registry_path))
        return cls({}, base_dir=registry_path.parent, registry_path=registry_path)

    # ----- mutation (character studio) ---------------------------------

    def upsert(self, asset: CharacterAsset) -> None:
        self._assets[asset.asset_id] = asset

    def remove(self, asset_id: str) -> bool:
        return self._assets.pop(asset_id, None) is not None

    def save(self, path: Optional[str] = None) -> str:
        """Persist the registry to yaml. Image paths are written relative to the
        registry's directory when possible so the asset library stays portable."""
        target = Path(path) if path else self.registry_path
        if target is None:
            raise ValueError("No registry_path to save to; pass an explicit path.")
        base_dir = target.parent
        characters: Dict[str, dict] = {}
        for asset_id, asset in self._assets.items():
            data = asset.model_dump(exclude_none=True)
            data.pop("asset_id", None)  # the key carries the id
            data["assets"] = {view: self._relativize(base_dir, p) for view, p in (asset.assets or {}).items()}
            if not data["assets"]:
                data.pop("assets")
            for reference_set in data.get("reference_sets") or []:
                reference_set["images"] = {
                    view: self._relativize(base_dir, image_path)
                    for view, image_path in (reference_set.get("images") or {}).items()
                }
                reference_set["expressions"] = {
                    expression: self._relativize(base_dir, image_path)
                    for expression, image_path in (reference_set.get("expressions") or {}).items()
                }
            characters[asset_id] = data
        from utils.atomic import atomic_write_text
        atomic_write_text(target, yaml.safe_dump({"characters": characters}, allow_unicode=True, sort_keys=False))
        self.registry_path = target
        self.base_dir = base_dir
        return str(target)

    @staticmethod
    def _relativize(base_dir: Path, path: str) -> str:
        p = Path(path)
        try:
            return p.resolve().relative_to(base_dir.resolve()).as_posix()
        except ValueError:
            return str(p)

    @classmethod
    def from_config(cls, config: dict) -> Optional["CharacterAssetRegistry"]:
        """Build from a pipeline config's ``character_assets`` section.

        Returns ``None`` when fixed characters are disabled or unconfigured, so
        callers can treat the feature as fully optional.
        """
        section = (config or {}).get("character_assets") or {}
        if not section.get("enabled"):
            return None
        registry_path = section.get("registry_path")
        if not registry_path or not os.path.exists(registry_path):
            return None
        return cls.from_yaml(registry_path)

    @staticmethod
    def _resolve_path(base_dir: Path, raw_path: str) -> str:
        p = Path(raw_path)
        if p.is_absolute():
            return str(p)
        candidate = base_dir / p
        if candidate.exists():
            return str(candidate)
        # fall back to the path as given (relative to CWD / repo root)
        return str(p)

    # ----- lookup -------------------------------------------------------

    def __len__(self) -> int:
        return len(self._assets)

    def all(self) -> List[CharacterAsset]:
        return list(self._assets.values())

    def get(self, asset_id: Optional[str]) -> Optional[CharacterAsset]:
        if not asset_id:
            return None
        return self._assets.get(asset_id)

    def match(self, name: str, description: str = "") -> Optional[CharacterAsset]:
        """Best-effort match a scene character name to a fixed asset.

        First version: case-insensitive exact match on id / display_name /
        aliases, then substring containment either direction. Similarity-based
        matching (embeddings) is intentionally deferred.
        """
        if not name:
            return None
        needle = name.strip().lower()

        for asset in self._assets.values():
            candidates = {asset.asset_id, asset.display_name, *asset.aliases}
            if any(needle == c.strip().lower() for c in candidates if c):
                return asset

        for asset in self._assets.values():
            candidates = {asset.display_name, *asset.aliases}
            for c in candidates:
                if not c:
                    continue
                c_low = c.strip().lower()
                if c_low and (c_low in needle or needle in c_low):
                    return asset
        return None

    def match_characters(self, characters) -> Dict[str, str]:
        """Return {identifier_in_scene: asset_id} for characters that match.

        ``characters`` is an iterable of objects exposing ``identifier_in_scene``
        (and optionally ``static_features`` / ``dynamic_features``).
        """
        bindings: Dict[str, str] = {}
        for character in characters:
            identifier = getattr(character, "identifier_in_scene", None)
            if not identifier:
                continue
            description = " ".join(
                str(getattr(character, attr, "") or "")
                for attr in ("static_features", "dynamic_features")
            )
            asset = self.match(identifier, description)
            if asset is not None:
                bindings[identifier] = asset.asset_id
        return bindings
