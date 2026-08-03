CREATE TABLE assets (
    asset_id TEXT PRIMARY KEY,
    asset_type TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'global' CHECK (scope IN ('global', 'project')),
    project_id TEXT REFERENCES projects(project_id) ON DELETE CASCADE,
    display_name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    aliases_json TEXT NOT NULL DEFAULT '[]',
    tags_json TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL DEFAULT 'user',
    license TEXT,
    usage_scope TEXT NOT NULL DEFAULT 'private',
    record_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((scope = 'global' AND project_id IS NULL) OR (scope = 'project' AND project_id IS NOT NULL))
);

CREATE INDEX idx_assets_scope_type ON assets(scope, project_id, asset_type, updated_at DESC);

CREATE TABLE character_identities (
    identity_id TEXT PRIMARY KEY REFERENCES assets(asset_id) ON DELETE CASCADE,
    identity_profile_json TEXT NOT NULL DEFAULT '{}',
    default_reference_set_id TEXT,
    default_outfit_version_id TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE reference_sets (
    identity_id TEXT NOT NULL REFERENCES character_identities(identity_id) ON DELETE CASCADE,
    reference_set_id TEXT NOT NULL,
    outfit_version_id TEXT,
    is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
    record_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (identity_id, reference_set_id)
);

CREATE TABLE outfit_versions (
    identity_id TEXT NOT NULL REFERENCES character_identities(identity_id) ON DELETE CASCADE,
    outfit_version_id TEXT NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
    record_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (identity_id, outfit_version_id)
);

CREATE TABLE render_bindings (
    identity_id TEXT NOT NULL REFERENCES character_identities(identity_id) ON DELETE CASCADE,
    binding_id TEXT NOT NULL,
    binding_type TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    record_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (identity_id, binding_id)
);

CREATE INDEX idx_render_bindings_enabled
    ON render_bindings(identity_id, binding_type, enabled);
