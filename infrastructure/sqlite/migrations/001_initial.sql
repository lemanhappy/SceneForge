CREATE TABLE app_state (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE projects (
    project_id TEXT PRIMARY KEY,
    legacy_session_id TEXT NOT NULL UNIQUE,
    working_dir TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'idea',
    title TEXT NOT NULL DEFAULT '',
    stage TEXT NOT NULL DEFAULT 'created',
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    record_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_projects_updated_at ON projects(updated_at DESC);

CREATE TABLE reviews (
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    review_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    artifact_version TEXT NOT NULL DEFAULT 'v1',
    artifact_refs_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    PRIMARY KEY (project_id, review_id)
);

CREATE INDEX idx_reviews_project_status ON reviews(project_id, status, created_at);

CREATE TABLE generation_jobs (
    job_id TEXT PRIMARY KEY,
    project_id TEXT REFERENCES projects(project_id) ON DELETE SET NULL,
    job_type TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    idempotency_key TEXT,
    state TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    progress_current INTEGER NOT NULL DEFAULT 0 CHECK (progress_current >= 0),
    progress_total INTEGER NOT NULL DEFAULT 0 CHECK (progress_total >= 0),
    provider TEXT,
    model TEXT,
    remote_task_id TEXT,
    worker_id TEXT,
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts >= 1),
    request_payload_json TEXT NOT NULL,
    result_json TEXT,
    error_code TEXT,
    error_message TEXT,
    estimated_cost REAL,
    actual_cost REAL,
    next_attempt_at TEXT,
    cancel_requested_at TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_generation_jobs_claim
    ON generation_jobs(state, priority DESC, created_at);
CREATE INDEX idx_generation_jobs_project
    ON generation_jobs(project_id, created_at DESC);
CREATE UNIQUE INDEX uq_generation_jobs_active_idempotency
    ON generation_jobs(idempotency_key)
    WHERE idempotency_key IS NOT NULL
      AND state IN ('queued', 'running', 'waiting_provider', 'retry_wait', 'cancel_requested', 'interrupted');

CREATE TABLE provider_profiles (
    provider_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    media_type TEXT NOT NULL,
    capability_json TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (provider_id, model_id)
);

CREATE TABLE legacy_imports (
    source_sha256 TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    imported_projects INTEGER NOT NULL,
    imported_reviews INTEGER NOT NULL,
    imported_at TEXT NOT NULL
);
