CREATE TABLE shots (
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    scene_index INTEGER NOT NULL CHECK (scene_index >= 0),
    shot_index INTEGER NOT NULL CHECK (shot_index >= 0),
    readiness TEXT NOT NULL DEFAULT 'draft',
    input_hash TEXT NOT NULL DEFAULT '',
    stale_reason TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, scene_index, shot_index)
);

CREATE INDEX idx_shots_project_readiness
    ON shots(project_id, readiness, scene_index, shot_index);

CREATE TABLE artifacts (
    artifact_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    scene_index INTEGER NOT NULL CHECK (scene_index >= 0),
    shot_index INTEGER NOT NULL CHECK (shot_index >= 0),
    artifact_type TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    status TEXT NOT NULL CHECK (status IN ('active', 'stale', 'archived')),
    input_hash TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    activated_at TEXT,
    UNIQUE (project_id, scene_index, shot_index, artifact_type, version)
);

CREATE UNIQUE INDEX uq_artifacts_active_version
    ON artifacts(project_id, scene_index, shot_index, artifact_type)
    WHERE status = 'active';

CREATE INDEX idx_artifacts_shot_history
    ON artifacts(project_id, scene_index, shot_index, artifact_type, version DESC);

CREATE TABLE artifact_inputs (
    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
    input_name TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    PRIMARY KEY (artifact_id, input_name)
);
