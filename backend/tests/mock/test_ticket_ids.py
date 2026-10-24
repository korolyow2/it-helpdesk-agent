"""Verify sequential IDs through approval, concurrency, restart and lookup."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import sqlite3

from fastapi.testclient import TestClient
import pytest

from helpdesk.mock.api import create_app
from helpdesk.mock.init_db import upgrade_database


def prepare_ticket(client, session):
    """Approve a distinct request and return its exact execution arguments."""
    headers = {
        "X-Mock-Service-Key": "test-service-key",
        "Authorization": "Bearer test-token-1",
        "X-Session-Id": session,
    }
    body = {"title": "Keyboard failure", "description": "The keyboard stops responding."}
    proposal = client.post("/tickets", headers=headers, json=body)
    assert proposal.status_code == 200
    approval_id = proposal.json()["approval_id"]
    decision = client.post(
        f"/approvals/{approval_id}/decision", headers=headers, json={"decision": "approve"},
    )
    assert decision.status_code == 200
    return headers, {**body, "approval_id": approval_id}


def execute_ticket(client, request):
    """Execute the exact approved request and return the confirmed ticket ID."""
    headers, body = request
    response = client.post("/tickets", headers=headers, json=body)
    assert response.status_code == 200, response.json()
    return response.json()["ticket"]["id"]


def test_sequence_survives_restart_and_repeated_approval(client, mock_config):
    """Keep consecutive numbers across restarts without allocating on replay."""
    first = prepare_ticket(client, "first")
    assert execute_ticket(client, first) == "1"
    assert execute_ticket(client, first) == "1"
    with TestClient(create_app(**mock_config)) as restarted:
        assert execute_ticket(restarted, first) == "1"
        assert execute_ticket(restarted, prepare_ticket(restarted, "second")) == "2"
    assert execute_ticket(client, prepare_ticket(client, "third")) == "3"


def test_parallel_ticket_creation_allocates_unique_consecutive_numbers(client):
    """Serialize different creations and duplicate deliveries atomically."""
    requests = [prepare_ticket(client, f"parallel-{index}") for index in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda request: execute_ticket(client, request), requests * 2))
    assert sorted(set(ids), key=int) == ["1", "2", "3", "4"]
    assert ids[:4] == ids[4:]
    assert execute_ticket(client, prepare_ticket(client, "next")) == "5"


@pytest.mark.parametrize("query", ["1", "check 1", "ticket 1", "Ticket ID 1", "#1"])
def test_numeric_id_can_be_searched_read_and_commented(client, query):
    """Use generated IDs across the same public read and write boundaries."""
    request = prepare_ticket(client, "lookup")
    assert execute_ticket(client, request) == "1"
    headers = request[0]
    found = client.get("/tickets", headers=headers, params={"query": query}).json()
    assert found["total"] == 1 and found["items"][0]["id"] == "1"
    assert client.get("/tickets/1", headers=headers).status_code == 200
    proposal = client.post("/tickets/1/comments", headers=headers, json={"body": "Still broken."})
    assert proposal.status_code == 200
    assert proposal.json()["arguments"]["ticket_id"] == "1"
    approval_id = proposal.json()["approval_id"]
    assert client.post(
        f"/approvals/{approval_id}/decision", headers=headers, json={"decision": "approve"},
    ).status_code == 200
    result = client.post(
        "/tickets/1/comments", headers=headers, json={"body": "Still broken.", "approval_id": approval_id},
    )
    assert result.status_code == 200 and result.json()["ticket_id"] == "1"


def test_numbers_in_symptoms_are_not_treated_as_ticket_ids(client):
    """Keep ordinary version and duration numbers in keyword search."""
    request = prepare_ticket(client, "keywords")
    execute_ticket(client, request)
    result = client.get("/tickets", headers=request[0], params={"query": "VPN fails after 1 minute"}).json()
    assert any(item["id"] == "TKT-001" for item in result["items"])


def test_numeric_ids_retain_ticket_permissions(client, database):
    """Numeric references cannot disclose restricted records or stage comments."""
    request = prepare_ticket(client, "restricted")
    execute_ticket(client, request)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE tickets SET visibility = 'restricted' WHERE id = '1'")
    headers = request[0]
    assert client.get("/tickets/1", headers=headers).status_code == 404
    assert client.get("/tickets", headers=headers, params={"query": "1"}).json()["total"] == 0
    assert client.post("/tickets/1/comments", headers=headers, json={"body": "Update"}).status_code == 404
    support = {**headers, "Authorization": "Bearer test-token-3"}
    assert client.get("/tickets/1", headers=support).status_code == 200


def test_upgrade_preserves_legacy_ids_and_existing_numeric_high_watermark(database):
    """Migrate old databases and never reset a previously used sequence."""
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("DROP TABLE ticket_sequence")
        connection.execute("UPDATE tickets SET id = '12' WHERE id = 'TKT-008'")
    upgrade_database(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        assert connection.execute("SELECT last_number FROM ticket_sequence").fetchone()[0] == 12
        assert connection.execute("SELECT COUNT(*) FROM tickets WHERE id = 'TKT-001'").fetchone()[0] == 1
        connection.execute("DELETE FROM tickets WHERE id = '12'")
    upgrade_database(database)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT last_number FROM ticket_sequence").fetchone()[0] == 12


def test_numeric_ids_sort_numerically(client):
    """Keep pagination stable when the sequence reaches two digits."""
    for index in range(12):
        request = prepare_ticket(client, f"ordering-{index}")
        execute_ticket(client, request)
    result = client.get(
        "/tickets", headers=request[0], params={"query": "Keyboard failure", "page_size": 50},
    ).json()
    assert [item["id"] for item in result["items"]] == [str(number) for number in range(1, 13)]
