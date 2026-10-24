"""Create a new mock database and load the fixture without overwriting existing data."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from helpdesk.paths import DATA_DIR, DEFAULT_DATABASE


def initialize_ticket_sequence(connection: sqlite3.Connection) -> None:
    """Preserve the counter and advance it past existing numeric ticket IDs.

    Parameters
    ----------
    connection : sqlite3.Connection
        Active schema initialization or upgrade transaction.
    """
    connection.execute("""
        INSERT INTO ticket_sequence (singleton, last_number)
        SELECT 1, COALESCE(MAX(CAST(id AS INTEGER)), 0) FROM tickets
        WHERE id != '' AND id NOT GLOB '*[^0-9]*'
        ON CONFLICT(singleton) DO UPDATE
        SET last_number = MAX(ticket_sequence.last_number, excluded.last_number)
    """)


def initialize_database(database: Path, seed: Path) -> tuple[int, int]:
    """Create the schema and load seed records in one transaction.

    Parameters
    ----------
    database : pathlib.Path
        Output SQLite path. Existing tables are not overwritten.
    seed : pathlib.Path
        JSON fixture containing users and tickets.

    Returns
    -------
    tuple of int
        Numbers of inserted users and tickets, respectively.

    Raises
    ------
    OSError
        If a source file or output directory cannot be accessed.
    ValueError
        If the fixture is not valid JSON.
    KeyError
        If required fixture fields are missing.
    sqlite3.Error
        If schema creation or insertion fails. Database changes are rolled back.
    """
    fixture = json.loads(seed.read_text(encoding="utf-8"))
    schema = (DATA_DIR / "schema.sql").read_text(encoding="utf-8")
    schema += "\n" + (DATA_DIR / "approval_schema.sql").read_text(encoding="utf-8")
    schema += "\n" + (DATA_DIR / "ticket_sequence_schema.sql").read_text(encoding="utf-8")
    tickets = [
        {**ticket, "comments": json.dumps(ticket.get("comments", []))}
        for ticket in fixture["tickets"]
    ]
    database.parent.mkdir(parents=True, exist_ok=True)

    with closing(sqlite3.connect(database)) as connection:
        # SQLite requires foreign key enforcement on every connection.
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            # executescript does not open a transaction; include schema and data in one.
            connection.executescript("BEGIN IMMEDIATE;\n" + schema)
            connection.executemany(
                "INSERT INTO users (id, name, role) VALUES (:id, :name, :role)",
                fixture["users"],
            )
            connection.executemany(
                """INSERT INTO tickets (
                    id, requester_id, title, description, status,
                    visibility, created_at, comments
                ) VALUES (
                    :id, :requester_id, :title, :description, :status,
                    :visibility, :created_at, :comments
                )""",
                tickets,
            )
            initialize_ticket_sequence(connection)

    return len(fixture["users"]), len(tickets)


def upgrade_database(database: Path) -> None:
    """Upgrade approval and sequence storage without replacing existing data.

    Parameters
    ----------
    database : pathlib.Path
        Existing initialized mock database.
    """
    schema = (DATA_DIR / "approval_schema.sql").read_text(encoding="utf-8")
    schema += "\n" + (DATA_DIR / "ticket_sequence_schema.sql").read_text(encoding="utf-8")
    with closing(sqlite3.connect(f"{database.resolve().as_uri()}?mode=rw", uri=True)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            connection.executescript("BEGIN IMMEDIATE;\n" + schema)
            initialize_ticket_sequence(connection)


def main() -> None:
    """Parse initialization arguments and report the result to the terminal."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--seed", type=Path, default=DATA_DIR / "seed.json")
    parser.add_argument("--upgrade", action="store_true", help="Upgrade storage without reseeding.")
    args = parser.parse_args()
    try:
        if args.upgrade:
            upgrade_database(args.database)
            print(f"Updated mock schema in {args.database}.")
            return
        users, tickets = initialize_database(args.database, args.seed)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        parser.exit(1, f"Database initialization failed: {error}\n")
    print(f"Created {args.database}: {users} users, {tickets} tickets.")


if __name__ == "__main__":
    main()
