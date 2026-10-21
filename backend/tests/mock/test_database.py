"""Verify that approval schema upgrades preserve an existing mock database."""

from contextlib import closing
import sqlite3

import pytest

from helpdesk.mock.init_db import upgrade_database


def test_upgrade_preserves_existing_records_and_is_repeatable(database):
    """Upgrade a legacy schema twice without resetting users, tickets, or comments."""
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("DROP TABLE approvals")
        connection.execute("UPDATE tickets SET title = 'Existing edited title' WHERE id = 'TKT-001'")
        before = connection.execute("SELECT * FROM tickets ORDER BY id").fetchall()
        users = connection.execute("SELECT * FROM users ORDER BY id").fetchall()
    upgrade_database(database)
    upgrade_database(database)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT * FROM tickets ORDER BY id").fetchall() == before
        assert connection.execute("SELECT * FROM users ORDER BY id").fetchall() == users
        assert connection.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_upgrade_does_not_create_a_missing_database(tmp_path):
    """Require explicit initialization rather than creating an empty replacement."""
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        upgrade_database(path)
    assert not path.exists()
