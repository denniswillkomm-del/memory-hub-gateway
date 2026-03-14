-- OAuth 2.0 authorization codes for MCP-over-SSE (Claude web)
CREATE TABLE IF NOT EXISTS oauth_codes (
    code            TEXT PRIMARY KEY,
    client_id       TEXT NOT NULL,
    redirect_uri    TEXT NOT NULL,
    code_challenge  TEXT,          -- PKCE SHA-256 challenge
    scope           TEXT,
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    used            INTEGER NOT NULL DEFAULT 0
);
