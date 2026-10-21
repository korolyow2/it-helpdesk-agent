from contextlib import closing
import sqlite3

import pytest


def headers(user_number):
    return {
        "X-Mock-Service-Key": "test-service-key",
        "Authorization": f"Bearer test-token-{user_number}",
    }


@pytest.mark.parametrize("user_number", [1, 2, 3, 4])
@pytest.mark.parametrize("ticket_number", range(1, 9))
def test_ticket_permissions(client, user_number, ticket_number):
    response = client.get(f"/tickets/TKT-{ticket_number:03}", headers=headers(user_number))
    if user_number == 4:
        assert response.status_code == 403
    elif ticket_number > 6 and user_number != 3:
        assert response.status_code == 404
        assert "RESTRICTED-" not in response.text
    else:
        assert response.status_code == 200
        assert response.json()["id"] == f"TKT-{ticket_number:03}"
        assert isinstance(response.json()["comments"], list)


@pytest.mark.parametrize(
    "request_headers",
    [
        {},
        {"Authorization": "Bearer test-token-1"},
        {"X-Mock-Service-Key": "test-service-key"},
        {**headers(1), "X-Mock-Service-Key": "wrong-key"},
        {**headers(1), "Authorization": "Bearer unknown-token"},
        {**headers(1), "Authorization": "Basic test-token-1"},
    ],
)
def test_rejects_missing_or_invalid_credentials(client, request_headers):
    response = client.get("/tickets/TKT-001", headers=request_headers)
    assert response.status_code == 401
    assert "description" not in response.json()


def test_missing_and_restricted_tickets_are_indistinguishable(client):
    restricted = client.get("/tickets/TKT-007", headers=headers(1))
    missing = client.get("/tickets/TKT-999", headers=headers(1))
    assert restricted.status_code == missing.status_code == 404
    restricted_error, missing_error = restricted.json()["error"], missing.json()["error"]
    assert restricted_error == missing_error


def test_read_returns_ticket_details_and_decoded_comments(client):
    response = client.get("/tickets/TKT-001", headers=headers(1))
    assert response.status_code == 200
    ticket = response.json()
    assert ticket["title"] == "VPN disconnects after verification"
    assert ticket["status"] == "open"
    assert len(ticket["comments"]) == 2
    assert ticket["comments"][0]["author_id"] == "user-003"
    assert ticket["comments"][1]["author_id"] == "user-001"


def test_openapi_requires_both_service_and_user_authentication(client):
    security = client.get("/openapi.json").json()["paths"]["/tickets/{ticket_id}"]["get"]["security"]
    assert security == [{"MockServiceKey": [], "MockUserToken": []}]


def test_user_and_role_headers_cannot_elevate_access(client):
    response = client.get(
        "/tickets/TKT-007?user_id=user-003&role=support",
        headers={**headers(1), "X-User-Id": "user-003", "X-Role": "support"},
    )
    assert response.status_code == 404


def test_permissions_are_read_from_database_on_each_request(client, database):
    assert client.get("/tickets/TKT-007", headers=headers(3)).status_code == 200
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE users SET role = 'no_helpdesk_access' WHERE id = 'user-003'")
    assert client.get("/tickets/TKT-007", headers=headers(3)).status_code == 403


def test_deleted_user_is_rejected(client, database):
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("DELETE FROM users WHERE id = 'user-004'")
    assert client.get("/tickets/TKT-001", headers=headers(4)).status_code == 401


@pytest.mark.parametrize("query,expected", [
    ("TKT-001", ["TKT-001"]),
    ("Please check tkt-002", ["TKT-002"]),
    ("TKT-001, tkt-002 and TKT-001", ["TKT-001", "TKT-002"]),
    ("VPN TKT-999", []),
    ("VPN TKT-007", []),
])
def test_exact_ticket_id_search(client, query, expected):
    """Prefer exact IDs in user text without falling back to unrelated keywords."""
    response = client.get("/tickets", headers=headers(1), params={"query": query})
    assert response.status_code == 200
    assert [ticket["id"] for ticket in response.json()["items"]] == expected
    assert response.json()["total"] == len(expected)


def test_exact_id_search_retains_support_access_and_pagination(client):
    """Filter hidden IDs before computing employee counts and pages."""
    query = "TKT-001 TKT-002 TKT-007 TKT-008"
    employee = client.get("/tickets", headers=headers(1), params={"query": query, "page_size": 1, "page": 2}).json()
    support = client.get("/tickets", headers=headers(3), params={"query": query}).json()
    assert employee["total"] == 2 and employee["has_more"] is False
    assert employee["items"][0]["id"] == "TKT-002"
    assert support["total"] == 4


@pytest.mark.parametrize("ticket_id", ["tkt-001", " TKT-001 "])
def test_direct_id_lookup_normalizes_case_and_whitespace(client, ticket_id):
    """Accept an ID without requiring the user to search first."""
    response = client.get(f"/tickets/{ticket_id}", headers=headers(1))
    assert response.status_code == 200
    assert response.json()["id"] == "TKT-001"


def test_validation_response_does_not_echo_private_inputs(client):
    """Expose safe schema field names rather than rejected text or unknown keys."""
    response = client.post("/tickets", headers={**headers(1), "X-Session-Id": "session"},
                           json={"title": "", "description": "SECRET-VALUE", "SECRET-KEY": "PRIVATE"})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_arguments"
    assert set(error) == {"is_error", "code", "message", "is_retryable", "outcome"}
    assert "SECRET" not in response.text
    assert "PRIVATE" not in response.text
    assert response.headers["X-Request-Id"]
