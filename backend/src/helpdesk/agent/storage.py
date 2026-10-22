"""Persist application approval bindings and one explicitly stated user fact."""

from contextlib import closing, contextmanager
import json
from pathlib import Path
import re
import sqlite3
import time

from helpdesk.errors import PublicError
from helpdesk.paths import DATA_DIR


FACT_TTL_SECONDS = 30 * 24 * 60 * 60
OS_STATEMENT = re.compile(
    r"^(?:(?:please )?(?:remember|note) (?:that )?)?"
    r"(?:I (?:use|run|am using|am on|work on)|I'm on|"
    r"my (?:work )?(?:computer|laptop|desktop|machine) (?:runs|uses|is on)) "
    r"(?:an? )?(Windows|macOS|Linux)\b", re.IGNORECASE,
)


class AppStorage:
    """Keep user-scoped state beside graph checkpoints in the agent database.

    Parameters
    ----------
    database : pathlib.Path
        Agent database; never the mock ticket database.
    """

    def __init__(self, database):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript((DATA_DIR / "agent_state_schema.sql").read_text())

    @contextmanager
    def connect(self):
        """Open a short transaction and close the SQLite connection reliably."""
        with closing(sqlite3.connect(self.database, timeout=5)) as connection:
            connection.row_factory = sqlite3.Row
            with connection:
                yield connection

    def action(self, user_id, session_id, *, call_key=None, approval_id=None):
        """Find an action only within its authenticated user and session."""
        with self.connect() as connection:
            condition, value = ("call_key", call_key) if call_key is not None else ("approval_id", approval_id)
            row = connection.execute(
                f"SELECT * FROM app_actions WHERE user_id = ? AND session_id = ? AND {condition} = ?",
                (user_id, session_id, value),
            ).fetchone()
        return self.decode(row)

    @staticmethod
    def decode(row):
        """Decode only application-owned JSON fields from an action record."""
        if row is None:
            return None
        return {**dict(row), "proposal": json.loads(row["proposal"]),
                "result": json.loads(row["result"]) if row["result"] else None}

    def latest_action(self, user_id, session_id):
        """Recover the most recent action without accepting a model-supplied actor."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM app_actions WHERE user_id = ? AND session_id = ? ORDER BY rowid DESC LIMIT 1",
                (user_id, session_id),
            ).fetchone()
        return self.decode(row)

    def save_action(self, user_id, session_id, call_key, proposal):
        """Bind a model-selected call to the backend's exact durable proposal."""
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO app_actions (user_id, session_id, call_key, approval_id, proposal) VALUES (?, ?, ?, ?, ?)",
                (user_id, session_id, call_key, proposal["approval_id"], json.dumps(proposal)),
            )
        return self.action(user_id, session_id, call_key=call_key)

    def decide(self, user_id, session_id, approval_id, decision):
        """Persist a verified next-message decision independently of model output."""
        action = self.action(user_id, session_id, approval_id=approval_id)
        if action is None:
            raise PublicError("approval_not_found")
        if action["decision"] not in {None, decision}:
            raise PublicError("approval_state_conflict")
        with self.connect() as connection:
            connection.execute(
                "UPDATE app_actions SET decision = ? WHERE user_id = ? AND session_id = ? AND approval_id = ?",
                (decision, user_id, session_id, approval_id),
            )

    def finish(self, user_id, session_id, approval_id, result):
        """Retain bounded outcome evidence for recovery, not an authorization grant."""
        receipt = {key: result[key] for key in ("status", "is_error", "code", "outcome") if key in result}
        with self.connect() as connection:
            connection.execute(
                "UPDATE app_actions SET result = ? WHERE user_id = ? AND session_id = ? AND approval_id = ?",
                (json.dumps(receipt), user_id, session_id, approval_id),
            )

    def learn(self, user_id, message, *, now=None):
        """Remember only an explicit first-person OS statement from accepted input."""
        sentences = re.split(r"(?<=[.!?])\s+|\n", message.strip())
        matches = [match for sentence in sentences if not sentence.rstrip().endswith("?")
                   and (match := OS_STATEMENT.search(sentence.strip()))]
        if not matches:
            return
        operating_system = {"windows": "Windows", "macos": "macOS", "linux": "Linux"}[matches[-1][1].lower()]
        timestamp = int(time.time() if now is None else now)
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO app_user_facts VALUES (?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET "
                "operating_system=excluded.operating_system, updated_at=excluded.updated_at, expires_at=excluded.expires_at",
                (user_id, operating_system, timestamp, timestamp + FACT_TTL_SECONDS),
            )

    def facts(self, user_id, *, now=None):
        """Read this user's unexpired fact and remove expired facts on access."""
        timestamp = int(time.time() if now is None else now)
        with self.connect() as connection:
            connection.execute("DELETE FROM app_user_facts WHERE expires_at <= ?", (timestamp,))
            row = connection.execute("SELECT operating_system, updated_at, expires_at FROM app_user_facts WHERE user_id = ?",
                                     (user_id,)).fetchone()
        return dict(row) if row else {}

    def forget(self, user_id):
        """Delete only the authenticated user's extracted fact, not conversation history."""
        with self.connect() as connection:
            connection.execute("DELETE FROM app_user_facts WHERE user_id = ?", (user_id,))
