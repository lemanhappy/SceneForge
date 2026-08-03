ALTER TABLE generation_jobs ADD COLUMN remote_provider TEXT;
ALTER TABLE generation_jobs ADD COLUMN remote_metadata_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE generation_jobs ADD COLUMN remote_artifact_path TEXT;

CREATE INDEX idx_generation_jobs_remote_recovery
    ON generation_jobs(state, remote_provider, updated_at);
