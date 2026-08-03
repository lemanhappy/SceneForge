"""Shared fixed-character injection helpers used by both pipelines.

Keeping these here (rather than on one pipeline) lets Script2VideoPipeline and
Idea2VideoPipeline reuse one implementation, so a scene character bound to a
fixed asset gets an identical registry entry regardless of entry point.
"""

import os
import shutil
from typing import Dict, Optional


def resolve_fixed_asset(asset_registry, character_bindings: Optional[Dict[str, str]], identifier: str):
    """Return the bound reference-image CharacterAsset for a scene character, or
    None when no usable fixed asset is bound."""
    if not asset_registry:
        return None
    asset = asset_registry.get((character_bindings or {}).get(identifier))
    images = asset.reference_images() if asset is not None and hasattr(asset, "reference_images") else (
        getattr(asset, "assets", {}) if asset is not None else {}
    )
    if asset is None or not images:
        return None
    return asset


def build_fixed_registry_entry(identifier: str, asset, character_dir: str) -> Dict[str, Dict[str, Dict[str, str]]]:
    """Copy a fixed asset's images into the session and emit a registry entry
    structurally identical to the auto-generated one, so every downstream
    consumer (reference selection, frame/video generation) works unchanged.

    The per-view description carries an explicit "keep appearance" constraint
    which flows into the reference-image selector prompt and helps lock the
    fixed character's look across shots.
    """
    os.makedirs(character_dir, exist_ok=True)
    entry: Dict[str, Dict[str, str]] = {}
    identity_constraint = (
        asset.visual_constraint()
        if hasattr(asset, "visual_constraint")
        else (asset.identity_constraint() if hasattr(asset, "identity_constraint") else "")
    )
    images = asset.reference_images() if hasattr(asset, "reference_images") else asset.assets
    for view, src_path in images.items():
        ext = os.path.splitext(src_path)[1] or ".png"
        dst = os.path.join(character_dir, f"{view}{ext}")
        if not os.path.exists(dst):
            shutil.copy(src_path, dst)
        entry[view] = {
            "path": dst,
            "description": (
                f"A {view} view of {identifier} ({asset.display_name}). "
                f"This is a FIXED character asset; you MUST keep this exact "
                f"appearance and not alter facial features, hairstyle, age or "
                f"core clothing. {asset.description}"
                f" {identity_constraint}"
            ).strip(),
        }
    return {identifier: entry}
