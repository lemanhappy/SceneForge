ALTER TABLE generation_jobs ADD COLUMN concurrency_key TEXT;

CREATE INDEX idx_generation_jobs_concurrency
    ON generation_jobs(concurrency_key, state, created_at DESC);

CREATE TABLE job_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES generation_jobs(job_id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    message TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX idx_job_events_job ON job_events(job_id, event_id);
