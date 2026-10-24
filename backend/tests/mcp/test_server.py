"""Exercise the real MCP HTTP/authentication stack against the mock API."""

from contextlib import asynccontextmanager, closing
import json
import sqlite3

from fastmcp.exceptions import ToolError
from fastmcp.utilities.tests import asgi_server
import httpx2 as httpx
import pytest

from helpdesk.mcp.mock_client import MockClient
from helpdesk.mcp.server import create_server
from helpdesk.telemetry import trace_headers


@asynccontextmanager
async def mcp_client(server, **kwargs):
    """Connect through the same stateless HTTP mode used by the runtime server.

    Parameters
    ----------
    server : FastMCP
        Server under test.
    **kwargs : dict
        Client authentication and request headers.

    Yields
    ------
    fastmcp.Client
        Connected MCP client using the complete HTTP middleware stack.
    """
    kwargs["headers"] = {**trace_headers(), **kwargs.get("headers", {})}
    async with asgi_server(server, stateless_http=True) as running:
        async with running.client(**kwargs) as client:
            yield client


@pytest.fixture
def server_options(mock_app):
    """Configure distinct synthetic MCP and mock credentials for all users."""
    return {
        "mock_url": "http://mock",
        "service_key": "test-service-key",
        "mcp_user_tokens": {f"mcp-token-{n}": f"user-{n:03}" for n in range(1, 5)},
        "mock_user_credentials": {f"user-{n:03}": f"test-token-{n}" for n in range(1, 5)},
        "transport": httpx.ASGITransport(app=mock_app),
    }


def human_decision(client, proposal, decision="approve"):
    """Deliver a human decision outside the five model-visible tools."""
    response = client.post(
        f"/approvals/{proposal['approval_id']}/decision",
        headers={"X-Mock-Service-Key": "test-service-key", "Authorization": "Bearer test-token-1",
                 "X-Session-Id": "mcp-session"},
        json={"decision": decision},
    )
    assert response.status_code == 200


async def test_discovery_and_search(server_options):
    """Discover exactly five tools and search authorized pages over MCP HTTP."""
    async with mcp_client(create_server(**server_options), auth="mcp-token-1") as mcp:
        tools = await mcp.list_tools()
        assert {tool.name for tool in tools} == {"list_tickets", "search_tickets", "get_ticket", "create_ticket", "add_comment"}
        for tool in tools:
            fields = tool.input_schema["properties"]
            assert not {"user_id", "role", "token", "service_key", "session_id"} & fields.keys()
        result = (await mcp.call_tool("search_tickets", {"query": "VPN", "page_size": 2})).data
        assert result["total"] == 4
        assert len(result["items"]) == 2
        assert result["has_more"] is True
        hidden = (await mcp.call_tool("search_tickets", {"query": "RESTRICTED-PAYROLL-CANARY-819"})).data
        assert hidden["items"] == []
        assert hidden["total"] == 0


@pytest.mark.parametrize("user,ticket,allowed", [(1, "TKT-001", True), (1, "TKT-007", False), (3, "TKT-007", True)])
async def test_read_permissions_through_mcp(server_options, user, ticket, allowed):
    """Retain backend ticket permissions through the MCP boundary."""
    async with mcp_client(create_server(**server_options), auth=f"mcp-token-{user}") as mcp:
        if allowed:
            assert (await mcp.call_tool("get_ticket", {"ticket_id": ticket})).data["id"] == ticket
        else:
            with pytest.raises(ToolError, match="not_found_or_inaccessible") as error:
                await mcp.call_tool("get_ticket", {"ticket_id": ticket})
            assert "RESTRICTED-VPN-CANARY-742" not in str(error.value)
            payload = json.loads(str(error.value))
            assert set(payload) == {"is_error", "code", "message", "is_retryable"}
            assert payload["is_error"] is True


@pytest.mark.parametrize("token", [None, "invalid-token", "test-token-1"])
async def test_mcp_rejects_invalid_or_unauthorized_identity(server_options, token):
    """Reject missing tokens, upstream token reuse, and users without Helpdesk."""
    app = create_server(**server_options).http_app(path="/mcp", stateless_http=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mcp") as client:
        response = await client.post("/mcp", headers={"Authorization": f"Bearer {token}"} if token else {},
                                     json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code in {401, 403}
    assert "search_tickets" not in response.text


@pytest.mark.parametrize("tool,arguments", [
    ("create_ticket", {"title": "Keyboard failure", "description": "The keyboard is not responding."}),
    ("add_comment", {"ticket_id": "TKT-001", "body": "The issue is still reproducible."}),
])
async def test_write_tools_require_human_approval_and_survive_reconnect(server_options, client, database, tool, arguments):
    """Complete both write tools only after explicit approval and replay safely."""
    async with mcp_client(create_server(**server_options), auth="mcp-token-1",
                           headers={"X-Session-Id": "mcp-session"}) as mcp:
        proposal = (await mcp.call_tool(tool, arguments)).data
        assert proposal["status"] == "pending"
        execution = {**arguments, "approval_id": proposal["approval_id"]}
        with pytest.raises(ToolError, match="approval_required"):
            await mcp.call_tool(tool, execution)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 8
        assert len(json.loads(connection.execute("SELECT comments FROM tickets WHERE id = 'TKT-001'").fetchone()[0])) == 2
    human_decision(client, proposal)
    # Build a new MCP server instance; the approval does not depend on its memory.
    async with mcp_client(create_server(**server_options), auth="mcp-token-1",
                           headers={"X-Session-Id": "mcp-session"}) as mcp:
        result = (await mcp.call_tool(tool, execution)).data
        assert result["status"] == "completed"
        assert (await mcp.call_tool(tool, execution)).data == result


async def test_declined_proposal_never_executes(server_options, client):
    """Keep a declined action non-executable through MCP."""
    async with mcp_client(create_server(**server_options), auth="mcp-token-1",
                           headers={"X-Session-Id": "mcp-session"}) as mcp:
        arguments = {"ticket_id": "TKT-001", "body": "Proposed comment"}
        proposal = (await mcp.call_tool("add_comment", arguments)).data
        human_decision(client, proposal, "decline")
        with pytest.raises(ToolError, match="approval_declined"):
            await mcp.call_tool("add_comment", {**arguments, "approval_id": proposal["approval_id"]})


async def test_model_cannot_choose_identity_or_approve(server_options):
    """Reject identity parameters and expose no tool that grants approval."""
    async with mcp_client(create_server(**server_options), auth="mcp-token-1") as mcp:
        with pytest.raises(ToolError):
            await mcp.call_tool("get_ticket", {"ticket_id": "TKT-007", "user_id": "user-003"})
        with pytest.raises(ToolError, match="session_required"):
            await mcp.call_tool("create_ticket", {"title": "Keyboard", "description": "Broken"})


async def test_mcp_rechecks_revoked_helpdesk_access(server_options, database):
    """Do not preserve privileged access after a database role is revoked."""
    async with mcp_client(create_server(**server_options), auth="mcp-token-3") as mcp:
        assert (await mcp.call_tool("get_ticket", {"ticket_id": "TKT-007"})).data["id"] == "TKT-007"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE users SET role = 'no_helpdesk_access' WHERE id = 'user-003'")
    async with mcp_client(create_server(**server_options), auth="mcp-token-3") as mcp:
        with pytest.raises(ToolError, match="access_denied"):
            await mcp.call_tool("get_ticket", {"ticket_id": "TKT-007"})
        with pytest.raises(Exception, match="access_denied"):
            await mcp.list_tools()


async def test_lost_write_responses_do_not_duplicate(server_options, client, database, mock_app):
    """Retry after both proposal and committed-write responses are lost."""
    upstream = httpx.ASGITransport(app=mock_app)
    failed = set()

    async def unreliable(request):
        """Return a transient error after the backend has already committed."""
        response = await upstream.handle_async_request(request)
        await response.aread()
        if request.method == "POST" and request.url.path == "/tickets":
            phase = bool(json.loads(request.content).get("approval_id"))
            if phase not in failed:
                failed.add(phase)
                await response.aclose()
                return httpx.Response(503, json={"detail": "Simulated lost response"})
        return response

    server_options["transport"] = httpx.MockTransport(unreliable)
    async with mcp_client(create_server(**server_options), auth="mcp-token-1",
                           headers={"X-Session-Id": "mcp-session"}) as mcp:
        body = {"title": "Keyboard", "description": "Unresponsive keys"}
        proposal = (await mcp.call_tool("create_ticket", body)).data
        human_decision(client, proposal)
        result = (await mcp.call_tool("create_ticket", {**body, "approval_id": proposal["approval_id"]})).data
        assert result["status"] == "completed"
        assert result["ticket"]["id"] == "1"
        assert (await mcp.call_tool("get_ticket", {"ticket_id": "1"})).data["id"] == "1"
    assert failed == {False, True}
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 9
        assert connection.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 1


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_retry_limit_and_safe_errors(status):
    """Bound retryable failures and exclude response content and credentials."""
    attempts = 0

    async def fail(request):
        """Return one retryable error without leaking its synthetic diagnostic body."""
        nonlocal attempts
        attempts += 1
        return httpx.Response(status, json={"detail": "PRIVATE-BACKEND-DIAGNOSTIC"})

    backend = MockClient("http://mock", "private-key", {"user-001": "private-token"},
                         httpx.MockTransport(fail))
    with pytest.raises(ToolError, match="rate_limited" if status == 429 else "backend_unavailable") as error:
        await backend.request("user-001", "GET", "/tickets")
    assert attempts == 3
    assert "PRIVATE" not in str(error.value)
    assert "private" not in str(error.value)


async def test_deadline_and_retry_after():
    """Honor a rate-limit wait and stop at the total deadline instead of retrying early."""
    attempts = 0

    async def rate_limit(request):
        """Request a wait longer than the remaining deadline."""
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, headers={"Retry-After": "5"})

    backend = MockClient("http://mock", "key", {"user-001": "token"},
                         httpx.MockTransport(rate_limit), deadline=0.03)
    with pytest.raises(ToolError, match="rate_limited") as error:
        await backend.request("user-001", "GET", "/tickets")
    assert json.loads(str(error.value))["retry_after_seconds"] == 5
    assert attempts == 1


async def test_slow_backend_is_bounded():
    """Cancel a slow transport when the operation budget expires."""
    import asyncio

    async def slow(request):
        """Wait beyond the operation's total budget."""
        await asyncio.sleep(1)
        return httpx.Response(200, json={})

    backend = MockClient("http://mock", "key", {"user-001": "token"},
                         httpx.MockTransport(slow), deadline=0.03)
    with pytest.raises(ToolError, match="deadline_exceeded"):
        await backend.request("user-001", "GET", "/tickets")


async def test_known_id_search_and_direct_read(server_options):
    """Normalize a known ID for direct reading and preserve exact search behavior."""
    async with mcp_client(create_server(**server_options), auth="mcp-token-1") as mcp:
        ticket = (await mcp.call_tool("get_ticket", {"ticket_id": " tkt-001 "})).data
        assert ticket["id"] == "TKT-001"
        result = (await mcp.call_tool("search_tickets", {"query": "Please check tkt-001"})).data
        assert result["total"] == 1 and result["items"][0]["id"] == "TKT-001"


async def test_backend_outage_does_not_become_invalid_identity(server_options):
    """Keep a valid MCP token distinct from an unavailable permission source."""
    async def unavailable(request):
        """Fail the live membership check without changing token validity."""
        return httpx.Response(503)

    server_options["transport"] = httpx.MockTransport(unavailable)
    async with mcp_client(create_server(**server_options), auth="mcp-token-1") as mcp:
        with pytest.raises(Exception, match="backend_unavailable"):
            await mcp.list_tools()
        with pytest.raises(ToolError, match="backend_unavailable"):
            await mcp.call_tool("get_ticket", {"ticket_id": "TKT-001"})
