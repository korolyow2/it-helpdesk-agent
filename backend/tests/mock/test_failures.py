"""Verify finite runtime faults independently of fake HTTP transport failures."""

from contextlib import closing
import sqlite3

import httpx2 as httpx
import pytest

from helpdesk.mcp.errors import ToolFailure
from helpdesk.mcp.mock_client import MockClient


def operator_headers():
    """Return a synthetic operator credential unavailable to the browser or model."""
    return {"X-Mock-Service-Key": "test-service-key"}


def ticket_headers():
    """Return independent service/user credentials for one fixture employee."""
    return {**operator_headers(), "Authorization": "Bearer test-token-1", "X-Session-Id": "failure-session"}


def test_failure_switch_requires_operator_and_preserves_authentication(client):
    """A browser token cannot activate faults or consume them without authorization."""
    assert client.put("/admin/failures", json={"mode": "server_error"}).status_code == 401
    assert client.put("/admin/failures", headers={"Authorization": "Bearer test-token-1"},
                      json={"mode": "server_error"}).status_code == 401
    assert client.put("/admin/failures", headers=operator_headers(), json={"mode": "server_error", "remaining": 1}).status_code == 200
    assert client.get("/tickets/TKT-001").status_code == 401
    assert client.get("/admin/failures", headers=operator_headers()).json()["remaining"] == 1
    assert client.get("/health").status_code == 200


@pytest.mark.parametrize("mode,status", [("rate_limit", 429), ("server_error", 503)])
def test_runtime_failures_are_finite_and_can_be_disabled(client, mode, status):
    """The running API recovers after the configured number of actual failures."""
    client.put("/admin/failures", headers=operator_headers(),
               json={"mode": mode, "remaining": 2, "retry_after_seconds": 0.01})
    replies = [client.get("/tickets/TKT-001", headers=ticket_headers()) for _ in range(3)]
    assert [item.status_code for item in replies] == [status, status, 200]
    if mode == "rate_limit":
        assert replies[0].headers["Retry-After"] == "0.01"
    client.put("/admin/failures", headers=operator_headers(), json={"mode": "off"})
    assert client.get("/tickets/TKT-001", headers=ticket_headers()).status_code == 200


@pytest.mark.parametrize("body", [{"mode": "unknown"}, {"remaining": -1}, {"remaining": 101},
                                 {"delay_seconds": 31}, {"retry_after_seconds": -1}])
def test_failure_parameters_are_bounded(client, body):
    """Reject unbounded or unsupported fault settings."""
    assert client.put("/admin/failures", headers=operator_headers(), json=body).status_code == 422


async def test_slow_response_respects_the_real_operation_deadline(client, mock_app):
    """The normal mock route sleeps; the production client ends within its bound."""
    client.put("/admin/failures", headers=operator_headers(), json={"mode": "slow", "remaining": 1, "delay_seconds": 0.2})
    backend = MockClient("http://mock", "test-service-key", {"user-001": "test-token-1"},
                         transport=httpx.ASGITransport(app=mock_app), deadline=0.05)
    with pytest.raises(ToolFailure) as failure:
        await backend.request("user-001", "GET", "/tickets/TKT-001")
    assert failure.value.error.code == "deadline_exceeded"


async def test_lost_committed_response_retries_exact_id_without_duplicate(client, mock_app, database):
    """A real route commits before its response is replaced by a synthetic timeout."""
    body = {"title": "Device test issue", "description": "The external display stops after docking."}
    proposal = client.post("/tickets", headers=ticket_headers(), json=body).json()
    assert client.post(f"/approvals/{proposal['approval_id']}/decision", headers=ticket_headers(),
                       json={"decision": "approve"}).status_code == 200
    client.put("/admin/failures", headers=operator_headers(),
               json={"mode": "committed_timeout", "target": "write", "remaining": 3})
    backend = MockClient("http://mock", "test-service-key", {"user-001": "test-token-1"}, transport=httpx.ASGITransport(app=mock_app))
    with pytest.raises(ToolFailure) as failure:
        await backend.request("user-001", "POST", "/tickets", session_id="failure-session",
                              json={**body, "approval_id": proposal["approval_id"]})
    assert failure.value.error.payload["outcome"] == "unknown"
    restored = await backend.request("user-001", "POST", "/tickets", session_id="failure-session",
                                     json={**body, "approval_id": proposal["approval_id"]})
    assert restored["status"] == "completed"
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 9
