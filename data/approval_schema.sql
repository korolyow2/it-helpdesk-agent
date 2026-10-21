CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id),
    session_id TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('create_ticket', 'add_comment')),
    payload TEXT NOT NULL CHECK (json_valid(payload)),
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'declined', 'completed')),
    result TEXT CHECK (result IS NULL OR json_valid(result)),
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_approval_per_session
ON approvals (user_id, session_id)
WHERE status IN ('pending', 'approved');
