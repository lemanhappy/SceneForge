from __future__ import annotations

import os
from pathlib import Path
from typing import MutableMapping


PRODUCT_NAME = "SceneForge"
STATE_DIR_NAME = ".sceneforge"
LEGACY_STATE_DIR_NAME = ".vimax"
ENV_PREFIX = "SCENEFORGE_"
LEGACY_ENV_PREFIX = "VIMAX_"


def apply_legacy_environment(
    environ: MutableMapping[str, str] | None = None,
) -> int:
    """Expose legacy ViMax settings under their SceneForge names."""

    values = os.environ if environ is None else environ
    migrated = 0
    for legacy_name, value in list(values.items()):
        if not legacy_name.startswith(LEGACY_ENV_PREFIX):
            continue
        current_name = ENV_PREFIX + legacy_name[len(LEGACY_ENV_PREFIX) :]
        if current_name not in values:
            values[current_name] = value
            migrated += 1
    return migrated


def state_directory(workspace_root: str | Path) -> Path:
    """Return the SceneForge state directory, migrating the old name once."""

    root = Path(workspace_root).resolve()
    current = root / STATE_DIR_NAME
    legacy = root / LEGACY_STATE_DIR_NAME
    if current.exists() or not legacy.exists():
        return current
    try:
        legacy.replace(current)
        return current
    except OSError:
        # A locked legacy directory remains usable until the next clean start.
        return legacy


apply_legacy_environment()
