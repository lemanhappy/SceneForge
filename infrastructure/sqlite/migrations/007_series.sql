CREATE TABLE series (
    series_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'completed', 'archived')),
    planned_episode_count INTEGER NOT NULL DEFAULT 1 CHECK (planned_episode_count >= 1),
    record_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_series_updated_at ON series(updated_at DESC);

ALTER TABLE projects ADD COLUMN series_id TEXT REFERENCES series(series_id) ON DELETE SET NULL;
ALTER TABLE projects ADD COLUMN episode_number INTEGER CHECK (episode_number IS NULL OR episode_number >= 1);
ALTER TABLE projects ADD COLUMN episode_title TEXT NOT NULL DEFAULT '';
ALTER TABLE projects ADD COLUMN previous_episode_id TEXT;

CREATE UNIQUE INDEX uq_projects_series_episode
    ON projects(series_id, episode_number)
    WHERE series_id IS NOT NULL AND episode_number IS NOT NULL;

CREATE INDEX idx_projects_series
    ON projects(series_id, episode_number, updated_at DESC);
