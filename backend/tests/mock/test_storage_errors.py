"""Verify safe storage failures and rollback classifications over HTTP."""

import sqlite3

from fastapi.testclient import TestClient
import pytest

from helpdesk.mock.store import TicketStore


@pytest.mark.parametrize("sqlite_code,expected,is_retryable", [
    (sqlite3.SQLITE_BUSY, "database_busy", True),
    (sqlite3.SQLITE_LOCKED, "database_busy", True),
    (sqlite3.SQLITE_FULL, "storage_unavailable", False),
    (sqlite3.SQLITE_READONLY, "storage_unavailable", False),
    (sqlite3.SQLITE_CORRUPT, "storage_unavailable", False),
    (sqlite3.SQLITE_IOERR, "storage_unavailable", False),
    (sqlite3.SQLITE_ERROR, "database_error", False),
])
def test_storage_failures_are_safe_and_actionable(mock_app, monkeypatch, sqlite_code, expected, is_retryable):
    """Separate retryable locks from storage faults that require an operator."""
    def fail(self, user_id, ticket_id):
        """Raise a typed storage failure without relying on message matching."""
        error = sqlite3.OperationalError("PRIVATE-DATABASE-PATH")
        error.sqlite_errorcode = sqlite_code
        raise error

    monkeypatch.setattr(TicketStore, "get_ticket", fail)
    with TestClient(mock_app, raise_server_exceptions=False) as client:
        response = client.get("/tickets/TKT-001", headers={"X-Mock-Service-Key": "test-service-key",
                                                          "Authorization": "Bearer test-token-1"})
    error = response.json()["error"]
    assert error["code"] == expected
    assert error["is_retryable"] is is_retryable
    assert set(error) == {"is_error", "code", "message", "is_retryable"}
    assert "PRIVATE" not in response.text
    assert response.headers["X-Request-Id"]


def test_unexpected_write_failure_reports_uncertain_outcome(mock_app, monkeypatch):
    """An unclassified failure cannot establish that a mutation did not commit."""
    def fail(self, *args):
        """Simulate an unexpected exception after entering the write operation."""
        raise RuntimeError("SECRET-INTERNAL-DETAIL")

    monkeypatch.setattr(TicketStore, "write", fail)
    with TestClient(mock_app, raise_server_exceptions=False) as client:
        response = client.post("/tickets", headers={"X-Mock-Service-Key": "test-service-key",
                               "Authorization": "Bearer test-token-1", "X-Session-Id": "session"},
                               json={"title": "Keyboard", "description": "Broken", "approval_id": "same-id"})
    error = response.json()["error"]
    assert response.status_code == 500
    assert error["code"] == "internal_error"
    assert error["outcome"] == "unknown"
    assert "SECRET" not in response.text
