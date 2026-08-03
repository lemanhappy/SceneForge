DROP INDEX uq_generation_jobs_active_idempotency;

CREATE UNIQUE INDEX uq_generation_jobs_active_idempotency
    ON generation_jobs(idempotency_key)
    WHERE idempotency_key IS NOT NULL
      AND state IN ('queued', 'running', 'waiting_provider', 'retry_wait', 'cancel_requested');

CREATE TABLE worker_leases (
    lease_name TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    heartbeat_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
