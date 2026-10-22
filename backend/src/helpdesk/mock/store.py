"""Apply ticket permissions and durable approvals inside SQLite transactions."""

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from helpdesk.errors import PublicError
from helpdesk.telemetry import observation


class TicketStore:
    """Access a seeded database with server-enforced roles and write approvals.

    Parameters
    ----------
    database : pathlib.Path
        Existing mock database with the approval schema applied.
    """

    def __init__(self, database: Path):
        """Store the database path without opening a connection."""
        self.database = database

    @contextmanager
    def connect(self, *, write=False):
        """Open a connection, optionally serializing a complete write transaction.

        Parameters
        ----------
        write : bool, optional
            Acquire the write lock before checking permissions and approval.

        Yields
        ------
        sqlite3.Connection
            Connection that commits on success and rolls back on an exception.
        """
        with closing(sqlite3.connect(f"{self.database.resolve().as_uri()}?mode=rw", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            with connection:
                connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield connection

    @staticmethod
    def authorize(connection, user_id):
        """Resolve the current database role or reject Helpdesk access.

        Parameters
        ----------
        connection : sqlite3.Connection
            Current transaction.
        user_id : str
            Identity derived from verified user credentials.

        Returns
        -------
        str
            The employee or support role.

        Raises
        ------
        PublicError
            For unknown users or users without Helpdesk access.
        """
        user = connection.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
        if user is None:
            raise PublicError("authentication_failed")
        if user["role"] not in {"employee", "support"}:
            raise PublicError("access_denied")
        return user["role"]

    @staticmethod
    def visible_ticket(connection, role, ticket_id):
        """Fetch an accessible ticket without revealing whether a hidden one exists.

        Parameters
        ----------
        connection : sqlite3.Connection
            Current transaction.
        role : str
            Verified role.
        ticket_id : str
            Requested ticket ID.

        Returns
        -------
        dict
            Ticket fields with decoded comments.

        Raises
        ------
        PublicError
            If the ticket is absent or inaccessible.
        """
        row = connection.execute(
            "SELECT * FROM tickets WHERE id = ? COLLATE NOCASE AND (visibility = 'internal' OR ? = 'support')",
            (ticket_id, role),
        ).fetchone()
        if row is None:
            raise PublicError("not_found_or_inaccessible")
        ticket = dict(row)
        ticket["comments"] = json.loads(ticket["comments"])
        return ticket

    def get_ticket(self, user_id, ticket_id):
        """Return an accessible ticket for the verified user.

        Parameters
        ----------
        user_id : str
            Authenticated user ID.
        ticket_id : str
            Requested ticket ID.

        Returns
        -------
        dict
            Ticket and comments.
        """
        with self.connect() as connection:
            return self.visible_ticket(connection, self.authorize(connection, user_id), ticket_id)

    def list_tickets(self, user_id, page, page_size):
        """List visible ticket names before applying pagination.

        Parameters
        ----------
        user_id : str
            Identity derived from verified credentials.
        page, page_size : int
            One-based page and maximum number of results.

        Returns
        -------
        dict
            IDs, titles, statuses, and pagination; ordered by ID.
        """
        with self.connect() as connection:
            role = self.authorize(connection, user_id)
            predicate = "visibility = 'internal' OR ? = 'support'"
            total = connection.execute(
                f"SELECT COUNT(*) FROM tickets WHERE {predicate}", (role,),
            ).fetchone()[0]
            rows = connection.execute(
                f"SELECT id, title, status FROM tickets WHERE {predicate} ORDER BY id LIMIT ? OFFSET ?",
                (role, page_size, (page - 1) * page_size),
            ).fetchall()

        return {
            "items": [dict(row) for row in rows], "total": total,
            "page": page, "page_size": page_size, "has_more": page * page_size < total,
        }

    def search(self, user_id, query, page, page_size):
        """Find visible exact IDs or search titles and descriptions by keywords.

        Parameters
        ----------
        user_id : str
            Authenticated user ID.
        query : str
            Exact ticket IDs or natural-language keywords with OR semantics.
        page : int
            One-based page number.
        page_size : int
            Maximum results on a page.

        Returns
        -------
        dict
            Summaries, total count, page metadata, and a has_more flag.

        Notes
        -----
        Title matches have higher weight. Ticket ID breaks score ties. Results
        are stable while data is unchanged; pagination is not a frozen snapshot.
        """
        ids = list(dict.fromkeys(value.upper() for value in re.findall(
            r"(?<![\w-])TKT-[A-Z0-9]+(?![\w-])", query, flags=re.IGNORECASE
        )))
        terms = list(dict.fromkeys(re.findall(r"[^\W_]+", query.lower())))
        if not terms:
            raise PublicError("invalid_arguments")
        score = " + ".join(
            "(3 * (instr(lower(title), ?) > 0) + (instr(lower(description), ?) > 0))"
            for _ in terms
        )
        parameters = [term for term in terms for _ in range(2)]
        with self.connect() as connection:
            role = self.authorize(connection, user_id)
            # Filter permissions before ranking, counts, and pagination.
            if ids:
                placeholders = ",".join("?" for _ in ids)
                sql = f"""SELECT id, title, status, visibility, 1 AS score FROM tickets
                    WHERE (visibility = 'internal' OR ? = 'support')
                    AND id COLLATE NOCASE IN ({placeholders})"""
                parameters = [role, *ids]
            else:
                sql = f"""WITH matches AS (
                    SELECT id, title, status, visibility, {score} AS score
                    FROM tickets WHERE visibility = 'internal' OR ? = 'support'
                ) SELECT * FROM matches WHERE score > 0"""
                parameters.append(role)
            total = connection.execute(f"SELECT COUNT(*) FROM ({sql})", parameters).fetchone()[0]
            rows = connection.execute(
                sql + " ORDER BY score DESC, id LIMIT ? OFFSET ?",
                [*parameters, page_size, (page - 1) * page_size],
            ).fetchall()
        return {
            "items": [{key: row[key] for key in ("id", "title", "status", "visibility")} for row in rows],
            "page": page, "page_size": page_size, "total": total,
            "has_more": page * page_size < total,
        }

    @staticmethod
    def approval(connection, approval_id, user_id, session_id):
        """Load an approval owned by the exact user and conversation.

        Parameters
        ----------
        connection : sqlite3.Connection
            Current transaction.
        approval_id, user_id, session_id : str
            Approval identifier and trusted ownership context.

        Returns
        -------
        sqlite3.Row
            Matching approval record.
        """
        row = connection.execute(
            "SELECT * FROM approvals WHERE id = ? AND user_id = ? AND session_id = ?",
            (approval_id, user_id, session_id),
        ).fetchone()
        if row is None:
            raise PublicError("approval_not_found")
        return row

    @staticmethod
    def proposal_result(row):
        """Serialize a proposal without exposing credentials or internal results.

        Parameters
        ----------
        row : sqlite3.Row
            Approval record.

        Returns
        -------
        dict
            Action, exact arguments, approval ID, and approval state.
        """
        return {
            "approval_id": row["id"], "status": row["status"],
            "operation": row["operation"], "arguments": json.loads(row["payload"]),
        }

    def get_approval(self, user_id, session_id, approval_id):
        """Restore a proposal for its authenticated owner and session.

        Parameters
        ----------
        user_id, session_id, approval_id : str
            Verified user, conversation, and requested approval.

        Returns
        -------
        dict
            Proposal state and exact action.
        """
        with self.connect() as connection:
            role = self.authorize(connection, user_id)
            row = self.approval(connection, approval_id, user_id, session_id)
            payload = json.loads(row["payload"])
            if row["operation"] == "add_comment":
                self.visible_ticket(connection, role, payload["ticket_id"])
            elif row["status"] == "completed":
                self.visible_ticket(connection, role, json.loads(row["result"])["ticket"]["id"])
            return self.proposal_result(row)

    def decide(self, user_id, session_id, approval_id, decision):
        """Record a user's explicit decision without executing the ticket write.

        Parameters
        ----------
        user_id, session_id, approval_id : str
            Verified user, conversation, and approval identifier.
        decision : {'approve', 'decline'}
            Decision delivered through the application, never an MCP tool.

        Returns
        -------
        dict
            Updated proposal state.
        """
        state = {"approve": "approved", "decline": "declined"}[decision]
        with self.connect(write=True) as connection:
            role = self.authorize(connection, user_id)
            row = self.approval(connection, approval_id, user_id, session_id)
            if row["operation"] == "add_comment":
                self.visible_ticket(connection, role, json.loads(row["payload"])["ticket_id"])
            if row["status"] not in {"pending", state}:
                raise PublicError("approval_state_conflict")
            connection.execute("UPDATE approvals SET status = ? WHERE id = ?", (state, approval_id))
            return self.proposal_result(self.approval(connection, approval_id, user_id, session_id))

    def write(self, user_id, session_id, operation, payload, approval_id=None):
        """Trace an approval-gated transaction and its durable idempotency result.

        Parameters
        ----------
        user_id, session_id, operation : str
            Verified actor, conversation, and requested operation.
        payload : dict
            Validated proposed arguments.
        approval_id : str or None, optional
            Approved action to execute, or absent when proposing.

        Returns
        -------
        dict
            Proposal or committed result, unchanged by tracing.
        """
        with observation(
            "write.transaction", input=payload,
            metadata={"operation": operation, "approval_id": approval_id, "acting_user": user_id},
        ) as span:
            result = self._write(user_id, session_id, operation, payload, approval_id, span)
            # Returning from _write means its connection context committed successfully.
            span.update(output=result, metadata={"transaction": "committed"})
            return result

    def _write(self, user_id, session_id, operation, payload, approval_id, span):
        """Propose a write or atomically execute an exact, explicitly approved action.

        Parameters
        ----------
        user_id, session_id : str
            Authenticated user and conversation.
        operation : {'create_ticket', 'add_comment'}
            Requested ticket operation.
        payload : dict
            Validated, normalized operation arguments.
        approval_id : str or None, optional
            Previously proposed action to execute after user approval.
        span : Observation
            Internal transaction diagnostics; never supplies authorization.

        Returns
        -------
        dict
            Pending proposal or durable completed result. Replaying a completed
            approval returns its result without repeating the mutation.

        Raises
        ------
        PublicError
            If access is denied, approval is missing, declined, belongs to
            another context, or does not match the requested action.
        """
        encoded = json.dumps(payload, sort_keys=True)
        with self.connect(write=True) as connection:
            role = self.authorize(connection, user_id)
            ticket = None
            if operation == "add_comment":
                ticket = self.visible_ticket(connection, role, payload["ticket_id"])
            if approval_id is None:
                row = connection.execute(
                    "SELECT * FROM approvals WHERE user_id = ? AND session_id = ? AND status IN ('pending', 'approved')",
                    (user_id, session_id),
                ).fetchone()
                if row is not None:
                    if row["operation"] != operation or row["payload"] != encoded:
                        raise PublicError("pending_approval_exists")
                    span.update(metadata={"write_phase": "proposal_reused"})
                    return self.proposal_result(row)
                approval_id = str(uuid4())
                connection.execute(
                    """INSERT INTO approvals (id, user_id, session_id, operation, payload, status, created_at)
                       VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
                    (approval_id, user_id, session_id, operation, encoded, datetime.now(timezone.utc).isoformat()),
                )
                span.update(metadata={"write_phase": "proposal_created"})
                return self.proposal_result(self.approval(connection, approval_id, user_id, session_id))

            row = self.approval(connection, approval_id, user_id, session_id)
            if row["operation"] != operation or row["payload"] != encoded:
                raise PublicError("approval_mismatch")
            if row["status"] == "completed":
                if operation == "create_ticket":
                    self.visible_ticket(connection, role, json.loads(row["result"])["ticket"]["id"])
                span.update(metadata={"write_phase": "idempotent_replay"})
                return json.loads(row["result"])
            if row["status"] != "approved":
                raise PublicError("approval_declined" if row["status"] == "declined" else "approval_required")

            span.update(metadata={"write_phase": "approved_mutation", "approval": "verified"})
            timestamp = datetime.now(timezone.utc).isoformat()
            if operation == "create_ticket":
                ticket_id = f"TKT-{uuid4().hex.upper()}"
                connection.execute(
                    """INSERT INTO tickets (id, requester_id, title, description, status, visibility, created_at)
                       VALUES (?, ?, ?, ?, 'open', 'internal', ?)""",
                    (ticket_id, user_id, payload["title"], payload["description"], timestamp),
                )
                result = {"ticket": self.visible_ticket(connection, role, ticket_id)}
            else:
                comment = {"id": f"CMT-{uuid4().hex}", "author_id": user_id,
                           "body": payload["body"], "created_at": timestamp}
                ticket["comments"].append(comment)
                connection.execute("UPDATE tickets SET comments = ? WHERE id = ? COLLATE NOCASE",
                                   (json.dumps(ticket["comments"]), payload["ticket_id"]))
                result = {"ticket_id": payload["ticket_id"], "comment": comment}
            result.update(status="completed", approval_id=approval_id)
            # The mutation and its replayable result commit together.
            connection.execute("UPDATE approvals SET status = 'completed', result = ? WHERE id = ?",
                               (json.dumps(result), approval_id))
            return result
