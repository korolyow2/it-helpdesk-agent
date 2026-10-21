"""Verify search visibility and durable, idempotent approval-gated writes."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import sqlite3

from fastapi.testclient import TestClient
import pytest

from helpdesk.mock.api import create_app


def headers(user=1, session="conversation-1"):
    """Return credentials and conversation context for a synthetic test user."""
    return {"X-Mock-Service-Key": "test-service-key", "Authorization": f"Bearer test-token-{user}",
            "X-Session-Id": session}


def decide(client, proposal, decision="approve", user=1, session="conversation-1"):
    """Submit a human decision through the non-tool application endpoint."""
    return client.post(f"/approvals/{proposal['approval_id']}/decision",
                       headers=headers(user, session), json={"decision": decision})


def test_search_filters_before_counts_and_pagination(client):
    """Ensure restricted matches never affect employee pages or counts."""
    pages = [client.get("/tickets", params={"query": "VPN", "page": p, "page_size": 2},
                        headers=headers()).json() for p in (1, 2, 3)]
    assert all(page["total"] == 4 for page in pages)
    assert [page["has_more"] for page in pages] == [True, False, False]
    ids = [item["id"] for page in pages for item in page["items"]]
    assert len(ids) == len(set(ids)) == 4
    assert set(ids) == {"TKT-001", "TKT-002", "TKT-003", "TKT-004"}
    assert client.get("/tickets", params={"query": "VPN"}, headers=headers(3)).json()["total"] == 5
    hidden = client.get("/tickets", params={"query": "RESTRICTED-PAYROLL-CANARY-819"}, headers=headers())
    assert hidden.json()["items"] == []
    assert hidden.json()["total"] == 0


@pytest.mark.parametrize("parameters", [
    {"query": ""}, {"query": "   "}, {"query": "%_"}, {"query": "VPN", "page": 0},
    {"query": "VPN", "page_size": 51}, {"query": "x" * 501},
    {"query": "VPN", "page": 10 ** 30},
])
def test_search_validates_arguments(client, parameters):
    """Reject invalid search input rather than reporting an empty successful result."""
    assert client.get("/tickets", params=parameters, headers=headers()).status_code == 422


def test_search_has_literal_keywords_and_no_matches(client):
    """Keep SQL syntax inert and distinguish a valid empty search from failure."""
    response = client.get("/tickets", params={"query": "zzzznonexistent"}, headers=headers())
    assert response.status_code == 200
    assert response.json()["items"] == []
    injection = client.get("/tickets", params={"query": "' OR 1=1; DROP TABLE tickets; --"}, headers=headers())
    assert injection.status_code == 200
    assert all(item["visibility"] == "internal" for item in injection.json()["items"])
    assert client.get("/tickets/TKT-001", headers=headers()).status_code == 200


@pytest.mark.parametrize("path,body", [
    ("/tickets", {"title": "Keyboard failure", "description": "The keyboard stops responding."}),
    ("/tickets/TKT-001/comments", {"body": "The issue still occurs after a restart."}),
])
def test_write_requires_approval_and_replays_once(client, path, body):
    """Proposals and decisions alone cannot mutate tickets; execution is replayable."""
    before = client.get("/tickets/TKT-001", headers=headers()).json()
    proposal = client.post(path, headers=headers(), json=body).json()
    assert proposal["status"] == "pending"
    assert client.post(path, headers=headers(), json=body).json() == proposal
    execution = {**body, "approval_id": proposal["approval_id"]}
    assert client.post(path, headers=headers(), json=execution).status_code == 409
    assert client.get("/tickets/TKT-001", headers=headers()).json() == before
    assert decide(client, proposal).json()["status"] == "approved"
    assert client.get("/tickets/TKT-001", headers=headers()).json() == before
    first = client.post(path, headers=headers(), json=execution)
    second = client.post(path, headers=headers(), json=execution)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["status"] == "completed"
    if path == "/tickets":
        assert first.json()["ticket"]["requester_id"] == "user-001"
        assert first.json()["ticket"]["visibility"] == "internal"
    else:
        assert len(client.get("/tickets/TKT-001", headers=headers()).json()["comments"]) == 3


def test_decline_and_changed_payload_cannot_write(client):
    """Reject declined actions and prevent approval reuse for modified arguments."""
    body = {"body": "Please investigate this issue."}
    path = "/tickets/TKT-001/comments"
    proposal = client.post(path, headers=headers(), json=body).json()
    decide(client, proposal, "decline")
    assert client.post(path, headers=headers(), json={**body, "approval_id": proposal["approval_id"]}).status_code == 409
    assert decide(client, proposal).status_code == 409
    proposal = client.post(path, headers=headers(), json=body).json()
    decide(client, proposal)
    assert client.post(path, headers=headers(), json={"body": "Changed text", "approval_id": proposal["approval_id"]}).status_code == 409
    assert client.post("/tickets/TKT-002/comments", headers=headers(),
                       json={**body, "approval_id": proposal["approval_id"]}).status_code == 409


def test_approval_is_private_to_user_and_session(client):
    """Even Support cannot read or approve another user's proposal."""
    proposal = client.post("/tickets", headers=headers(), json={"title": "Keyboard", "description": "Not working"}).json()
    for user, session in [(2, "conversation-1"), (3, "conversation-1"), (1, "another-session")]:
        assert decide(client, proposal, user=user, session=session).status_code == 404
        assert client.get(f"/approvals/{proposal['approval_id']}", headers=headers(user, session)).status_code == 404


def test_restart_preserves_pending_and_completed_approval(client, mock_config):
    """Recreate the service around the same database before and after execution."""
    body = {"title": "Keyboard", "description": "Not working"}
    proposal = client.post("/tickets", headers=headers(), json=body).json()
    with TestClient(create_app(**mock_config)) as restarted:
        assert restarted.get(f"/approvals/{proposal['approval_id']}", headers=headers()).json() == proposal
        decide(restarted, proposal)
        execution = {**body, "approval_id": proposal["approval_id"]}
        result = restarted.post("/tickets", headers=headers(), json=execution).json()
    with TestClient(create_app(**mock_config)) as restarted:
        assert restarted.post("/tickets", headers=headers(), json=execution).json() == result


@pytest.mark.parametrize("user,ticket,status", [(1, "TKT-007", 404), (2, "TKT-008", 404), (4, "TKT-001", 403)])
def test_forbidden_comment_does_not_even_create_proposal(client, database, user, ticket, status):
    """Enforce access before persisting or returning any proposed change."""
    response = client.post(f"/tickets/{ticket}/comments", headers=headers(user), json={"body": "Try writing"})
    assert response.status_code == status
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0


def test_support_can_comment_on_restricted_ticket(client):
    """Apply the same approval gate to Support's permitted restricted writes."""
    path = "/tickets/TKT-007/comments"
    body = {"body": "Restricted investigation update."}
    proposal = client.post(path, headers=headers(3), json=body).json()
    assert decide(client, proposal, user=3).status_code == 200
    result = client.post(path, headers=headers(3), json={**body, "approval_id": proposal["approval_id"]})
    assert result.json()["comment"]["author_id"] == "user-003"


def test_changed_permissions_block_approved_action(client, database):
    """Recheck ticket visibility and role after approval and before execution."""
    body = {"body": "Investigate again."}
    path = "/tickets/TKT-001/comments"
    proposal = client.post(path, headers=headers(), json=body).json()
    decide(client, proposal)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE tickets SET visibility = 'restricted' WHERE id = 'TKT-001'")
    assert client.post(path, headers=headers(), json={**body, "approval_id": proposal["approval_id"]}).status_code == 404


def test_parallel_execution_does_not_duplicate_comment(client):
    """Two concurrent executions of one approval commit exactly one comment."""
    path = "/tickets/TKT-001/comments"
    body = {"body": "Concurrent retry"}
    proposal = client.post(path, headers=headers(), json=body).json()
    decide(client, proposal)

    def execute(_):
        """Submit one duplicate execution request."""
        return client.post(path, headers=headers(), json={**body, "approval_id": proposal["approval_id"]})

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(execute, range(2)))
    assert all(response.status_code == 200 for response in responses)
    assert responses[0].json() == responses[1].json()
    assert len(client.get("/tickets/TKT-001", headers=headers()).json()["comments"]) == 3


def test_model_cannot_set_owner_role_or_visibility(client):
    """Reject ownership and permission fields in write payloads."""
    response = client.post("/tickets", headers=headers(), json={
        "title": "Test", "description": "Issue", "requester_id": "user-003", "visibility": "restricted",
    })
    assert response.status_code == 422


def test_failed_completion_rolls_back_the_ticket_write(client, mock_config, database):
    """Keep mutation and idempotency result atomic even if completion storage fails."""
    body = {"title": "Keyboard", "description": "Not working"}
    proposal = client.post("/tickets", headers=headers(), json=body).json()
    decide(client, proposal)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.executescript("""
            CREATE TRIGGER fail_completion BEFORE UPDATE OF status ON approvals
            WHEN NEW.status = 'completed' BEGIN
                SELECT RAISE(ABORT, 'Simulated completion storage failure');
            END;
        """)
    execution = {**body, "approval_id": proposal["approval_id"]}
    with TestClient(create_app(**mock_config), raise_server_exceptions=False) as failing:
        assert failing.post("/tickets", headers=headers(), json=execution).status_code == 409
    with closing(sqlite3.connect(database)) as connection, connection:
        assert connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 8
        assert connection.execute("SELECT status FROM approvals").fetchone()[0] == "approved"
        connection.execute("DROP TRIGGER fail_completion")
    assert client.post("/tickets", headers=headers(), json=execution).json()["status"] == "completed"


def test_normalized_comment_id_keeps_one_approved_action(client):
    """Normalize ticket IDs before binding the proposal and executing its payload."""
    proposal = client.post("/tickets/tkt-001/comments", headers=headers(), json={"body": "Still broken"}).json()
    assert proposal["arguments"]["ticket_id"] == "TKT-001"
    assert decide(client, proposal).status_code == 200
    result = client.post("/tickets/TKT-001/comments", headers=headers(),
                         json={"body": "Still broken", "approval_id": proposal["approval_id"]})
    assert result.status_code == 200
    assert len(client.get("/tickets/TKT-001", headers=headers()).json()["comments"]) == 3


def test_created_ticket_can_be_found_by_returned_id(client):
    """Search and read newly generated IDs as well as seeded short identifiers."""
    body = {"title": "Keyboard", "description": "Broken"}
    proposal = client.post("/tickets", headers=headers(), json=body).json()
    decide(client, proposal)
    result = client.post("/tickets", headers=headers(), json={**body, "approval_id": proposal["approval_id"]}).json()
    ticket_id = result["ticket"]["id"]
    found = client.get("/tickets", headers=headers(), params={"query": f"check {ticket_id.lower()}"}).json()
    assert found["total"] == 1 and found["items"][0]["id"] == ticket_id
    assert client.get(f"/tickets/{ticket_id.lower()}", headers=headers()).status_code == 200


def test_approval_errors_have_distinct_codes(client):
    """Distinguish missing, unapproved, changed, busy, and declined actions."""
    body = {"title": "Keyboard", "description": "Broken"}
    proposal = client.post("/tickets", headers=headers(), json=body).json()
    execution = {**body, "approval_id": proposal["approval_id"]}
    cases = [
        ({**body, "approval_id": "missing"}, "approval_not_found"),
        (execution, "approval_required"),
        ({**execution, "title": "Changed"}, "approval_mismatch"),
        ({**body, "title": "Another proposal"}, "pending_approval_exists"),
    ]
    for payload, code in cases:
        error = client.post("/tickets", headers=headers(), json=payload).json()["error"]
        assert error["code"] == code
        assert error["is_retryable"] is False and error["outcome"] == "not_applied"
    decide(client, proposal, "decline")
    error = client.post("/tickets", headers=headers(), json=execution).json()["error"]
    assert error["code"] == "approval_declined"
