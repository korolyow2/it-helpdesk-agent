CREATE TABLE users (
    id TEXT PRIMARY KEY NOT NULL,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('employee', 'support', 'no_helpdesk_access'))
);

CREATE TABLE tickets (
    id TEXT PRIMARY KEY NOT NULL,
    requester_id TEXT NOT NULL REFERENCES users(id),
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'in_progress', 'resolved')),
    visibility TEXT NOT NULL CHECK (visibility IN ('internal', 'restricted')),
    created_at TEXT NOT NULL,
    comments TEXT NOT NULL DEFAULT '[]' CHECK (
        CASE WHEN json_valid(comments) THEN json_type(comments) = 'array' ELSE 0 END
    )
);
