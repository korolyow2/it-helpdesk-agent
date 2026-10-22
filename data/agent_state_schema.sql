CREATE TABLE IF NOT EXISTS app_actions (
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    call_key TEXT NOT NULL,
    approval_id TEXT NOT NULL UNIQUE,
    proposal TEXT NOT NULL CHECK (json_valid(proposal)),
    decision TEXT CHECK (decision IN ('approve', 'decline')),
    result TEXT CHECK (result IS NULL OR json_valid(result)),
    PRIMARY KEY (user_id, session_id, call_key)
);

CREATE TABLE IF NOT EXISTS app_user_facts (
    user_id TEXT PRIMARY KEY NOT NULL,
    operating_system TEXT NOT NULL CHECK (operating_system IN ('Windows', 'macOS', 'Linux')),
    updated_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
