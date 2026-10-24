CREATE TABLE IF NOT EXISTS ticket_sequence (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    last_number INTEGER NOT NULL CHECK (last_number >= 0)
);
