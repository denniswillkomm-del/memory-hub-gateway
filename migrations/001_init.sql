PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS devices (
    device_id          TEXT PRIMARY KEY,
    hashed_refresh_token TEXT NOT NULL,
    device_name        TEXT,
    user_id            TEXT,
    registered_at      TEXT NOT NULL,
    last_seen          TEXT,
    refresh_token_expires_at TEXT,
    revoked            INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pairing_requests (
    request_id         TEXT PRIMARY KEY,
    device_id          TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending', 'approved', 'denied', 'expired')),
    refresh_token_plaintext TEXT,
    created_at         TEXT NOT NULL,
    expires_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approval_requests (
    request_id         TEXT PRIMARY KEY,
    idempotency_key    TEXT NOT NULL,
    tool_name          TEXT NOT NULL,
    arguments_hash     TEXT NOT NULL,
    state              TEXT NOT NULL DEFAULT 'pending'
                           CHECK (state IN ('pending', 'approved', 'denied', 'expired', 'executed', 'failed')),
    result             TEXT,
    error              TEXT,
    created_at         TEXT NOT NULL,
    expires_at         TEXT NOT NULL,
    result_expires_at  TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_requests_idempotency
    ON approval_requests(idempotency_key);

CREATE TABLE IF NOT EXISTS companion_heartbeats (
    device_id          TEXT PRIMARY KEY REFERENCES devices(device_id) ON DELETE CASCADE,
    last_heartbeat_at  TEXT NOT NULL
);
