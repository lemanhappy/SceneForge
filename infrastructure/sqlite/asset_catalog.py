from __future__ import annotations

import json
from datetime import datetime, timezone

from characters.models import CharacterAsset, ReusableAsset

from .database import SQLiteDatabase


class SQLiteAssetCatalogRepository:
    """Durable reusable-asset catalog; character YAML remains import-compatible."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database
        self.database.migrate()

    def upsert_character(
        self,
        asset: CharacterAsset,
        *,
        scope: str = "global",
        project_id: str | None = None,
    ) -> CharacterAsset:
        scope = str(scope or "global").strip().lower()
        if scope not in {"global", "project"}:
            raise ValueError("scope must be 'global' or 'project'")
        project_id = str(project_id).strip() if project_id is not None else None
        if (scope == "global" and project_id) or (scope == "project" and not project_id):
            raise ValueError("project scope requires project_id; global scope forbids it")

        now = _utc_now()
        record_json = _json(asset.model_dump(mode="json", exclude_none=True))
        with self.database.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT asset_type FROM assets WHERE asset_id = ?", (asset.asset_id,)
            ).fetchone()
            if existing and existing["asset_type"] != "character":
                raise ValueError(
                    f"asset_id '{asset.asset_id}' already belongs to {existing['asset_type']}"
                )
            connection.execute(
                """
                INSERT INTO assets(
                    asset_id, asset_type, scope, project_id, display_name,
                    description, aliases_json, tags_json, source, license,
                    usage_scope, record_json, created_at, updated_at
                ) VALUES (?, 'character', ?, ?, ?, ?, ?, '[]', 'user', NULL,
                          'private', ?, ?, ?)
                ON CONFLICT(asset_id) DO UPDATE SET
                    asset_type = excluded.asset_type,
                    scope = excluded.scope,
                    project_id = excluded.project_id,
                    display_name = excluded.display_name,
                    description = excluded.description,
                    aliases_json = excluded.aliases_json,
                    record_json = excluded.record_json,
                    updated_at = excluded.updated_at
                """,
                (
                    asset.asset_id,
                    scope,
                    project_id,
                    asset.display_name,
                    asset.description,
                    _json(asset.aliases),
                    record_json,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO character_identities(
                    identity_id, identity_profile_json, default_reference_set_id,
                    default_outfit_version_id, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(identity_id) DO UPDATE SET
                    identity_profile_json = excluded.identity_profile_json,
                    default_reference_set_id = excluded.default_reference_set_id,
                    default_outfit_version_id = excluded.default_outfit_version_id,
                    updated_at = excluded.updated_at
                """,
                (
                    asset.asset_id,
                    _json(asset.identity_profile.model_dump(mode="json")),
                    _default_id(asset.reference_sets),
                    _default_id(asset.outfit_versions),
                    now,
                ),
            )
            for table in ("reference_sets", "outfit_versions", "render_bindings"):
                connection.execute(f"DELETE FROM {table} WHERE identity_id = ?", (asset.asset_id,))
            connection.executemany(
                """
                INSERT INTO reference_sets(
                    identity_id, reference_set_id, outfit_version_id,
                    is_default, record_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        asset.asset_id,
                        item.reference_set_id,
                        item.outfit_version_id,
                        int(item.is_default),
                        _json(item.model_dump(mode="json", exclude_none=True)),
                        now,
                    )
                    for item in asset.reference_sets
                ],
            )
            connection.executemany(
                """
                INSERT INTO outfit_versions(
                    identity_id, outfit_version_id, is_default,
                    record_json, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        asset.asset_id,
                        item.outfit_version_id,
                        int(item.is_default),
                        _json(item.model_dump(mode="json", exclude_none=True)),
                        now,
                    )
                    for item in asset.outfit_versions
                ],
            )
            connection.executemany(
                """
                INSERT INTO render_bindings(
                    identity_id, binding_id, binding_type,
                    enabled, record_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        asset.asset_id,
                        item.binding_id,
                        item.kind,
                        int(item.enabled),
                        _json(item.model_dump(mode="json", exclude_none=True)),
                        now,
                    )
                    for item in asset.render_bindings
                ],
            )
        return asset

    def get_character(self, asset_id: str) -> CharacterAsset | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT record_json FROM assets WHERE asset_id = ? AND asset_type = 'character'",
                (str(asset_id),),
            ).fetchone()
        return CharacterAsset.model_validate_json(row["record_json"]) if row else None

    def list_characters(self, project_id: str | None = None) -> list[CharacterAsset]:
        with self.database.connection() as connection:
            if project_id is None:
                rows = connection.execute(
                    """
                    SELECT record_json FROM assets
                    WHERE asset_type = 'character' AND scope = 'global'
                    ORDER BY updated_at DESC, asset_id
                    """
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT record_json FROM assets
                    WHERE asset_type = 'character'
                      AND (scope = 'global' OR (scope = 'project' AND project_id = ?))
                    ORDER BY scope DESC, updated_at DESC, asset_id
                    """,
                    (str(project_id),),
                ).fetchall()
        return [CharacterAsset.model_validate_json(row["record_json"]) for row in rows]

    def remove_character(self, asset_id: str) -> bool:
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "DELETE FROM assets WHERE asset_id = ? AND asset_type = 'character'",
                (str(asset_id),),
            )
        return cursor.rowcount > 0

    def upsert_asset(
        self,
        asset: ReusableAsset,
        *,
        scope: str = "global",
        project_id: str | None = None,
    ) -> ReusableAsset:
        scope, project_id = _normalize_scope(scope, project_id)
        now = _utc_now()
        record_json = _json(asset.model_dump(mode="json", exclude_none=True))
        with self.database.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT asset_type FROM assets WHERE asset_id = ?", (asset.asset_id,)
            ).fetchone()
            if existing and existing["asset_type"] != asset.asset_type:
                raise ValueError(
                    f"asset_id '{asset.asset_id}' already belongs to {existing['asset_type']}"
                )
            connection.execute(
                """
                INSERT INTO assets(
                    asset_id, asset_type, scope, project_id, display_name,
                    description, aliases_json, tags_json, source, license,
                    usage_scope, record_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'user', NULL, 'private', ?, ?, ?)
                ON CONFLICT(asset_id) DO UPDATE SET
                    asset_type = excluded.asset_type,
                    scope = excluded.scope,
                    project_id = excluded.project_id,
                    display_name = excluded.display_name,
                    description = excluded.description,
                    aliases_json = excluded.aliases_json,
                    tags_json = excluded.tags_json,
                    record_json = excluded.record_json,
                    updated_at = excluded.updated_at
                """,
                (
                    asset.asset_id,
                    asset.asset_type,
                    scope,
                    project_id,
                    asset.display_name,
                    asset.description,
                    _json(asset.aliases),
                    _json(asset.tags),
                    record_json,
                    now,
                    now,
                ),
            )
        return asset

    def get_asset(self, asset_id: str) -> ReusableAsset | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT record_json FROM assets WHERE asset_id = ? AND asset_type IN ('prop', 'scene')",
                (str(asset_id),),
            ).fetchone()
        return ReusableAsset.model_validate_json(row["record_json"]) if row else None

    def list_assets(
        self, asset_type: str | None = None, project_id: str | None = None
    ) -> list[ReusableAsset]:
        if asset_type is not None and asset_type not in {"prop", "scene"}:
            raise ValueError("asset_type must be prop or scene")
        params: list[str] = []
        where = ["asset_type IN ('prop', 'scene')"]
        if asset_type:
            where.append("asset_type = ?")
            params.append(asset_type)
        if project_id is None:
            where.append("scope = 'global'")
            order = "updated_at DESC, asset_id"
        else:
            where.append("(scope = 'global' OR (scope = 'project' AND project_id = ?))")
            params.append(str(project_id))
            order = "scope DESC, updated_at DESC, asset_id"
        with self.database.connection() as connection:
            rows = connection.execute(
                f"SELECT record_json FROM assets WHERE {' AND '.join(where)} ORDER BY {order}",
                tuple(params),
            ).fetchall()
        return [ReusableAsset.model_validate_json(row["record_json"]) for row in rows]

    def remove_asset(self, asset_id: str) -> bool:
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "DELETE FROM assets WHERE asset_id = ? AND asset_type IN ('prop', 'scene')",
                (str(asset_id),),
            )
        return cursor.rowcount > 0


def _default_id(items) -> str | None:
    selected = next((item for item in items if item.is_default), None)
    if selected is None and items:
        selected = items[0]
    if selected is None:
        return None
    return getattr(selected, "reference_set_id", None) or getattr(selected, "outfit_version_id", None)


def _normalize_scope(scope: str, project_id: str | None) -> tuple[str, str | None]:
    normalized = str(scope or "global").strip().lower()
    if normalized not in {"global", "project"}:
        raise ValueError("scope must be 'global' or 'project'")
    project = str(project_id).strip() if project_id is not None else None
    if (normalized == "global" and project) or (normalized == "project" and not project):
        raise ValueError("project scope requires project_id; global scope forbids it")
    return normalized, project


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
