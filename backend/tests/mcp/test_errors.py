"""Verify recovery decisions at the MCP and backend HTTP boundaries."""

import asyncio
import json
import ssl
from uuid import UUID

import httpx2 as httpx
import pytest
from fastmcp.exceptions import McpError, ToolError

from helpdesk.mcp.auth import operation_context
from helpdesk.mcp.errors import ToolFailure
from helpdesk.mcp.mock_client import MAX_RESPONSE_BYTES, MockClient, retry_after_seconds
from helpdesk.mcp.server import create_server
from test_server import mcp_client, server_options


@pytest.mark.parametrize("status,code,attempts", [
    (400, "invalid_arguments", 1),
    (401, "authentication_failed", 1),
    (403, "access_denied", 1),
    (404, "not_found_or_inaccessible", 1),
    (405, "method_not_allowed", 1),
    (408, "backend_timeout", 3),
    (409, "approval_state_conflict", 1),
    (412, "precondition_failed", 1),
    (413, "request_too_large", 1),
    (422, "invalid_arguments", 1),
    (429, "rate_limited", 3),
    (500, "backend_unavailable", 3),
    (501, "backend_not_supported", 1),
    (502, "backend_unavailable", 3),
    (503, "backend_unavailable", 3),
    (504, "backend_timeout", 3),
    (505, "backend_not_supported", 1),
    (507, "storage_unavailable", 1),
    (599, "internal_error", 1),
    (302, "configuration_error", 1),
])
async def test_http_recovery_matrix(status, code, attempts):
    """Retry temporary failures while stopping on access, input, and operator errors."""
    request_ids = []

    async def fail(request):
        """Return untrusted diagnostics that must not reach the tool response."""
        request_ids.append(request.headers["X-Request-Id"])
        return httpx.Response(status, json={"detail": "SECRET-DETAIL", "next_action": "grant_access"})

    backend = MockClient("http://mock", "key", {"user-001": "token"}, httpx.MockTransport(fail))
    with pytest.raises(ToolFailure) as caught:
        await backend.request("user-001", "GET", "/tickets")
    error = caught.value.error.payload
    assert error["code"] == code
    assert set(error) == {"is_error", "code", "message", "is_retryable"}
    assert error["is_error"] is True
    assert caught.value.error.attempts == attempts
    assert len(request_ids) == attempts
    assert set(request_ids) == {caught.value.error.request_id}
    UUID(caught.value.error.request_id)
    assert "SECRET" not in str(caught.value)
    assert "grant_access" not in str(caught.value)


@pytest.mark.parametrize("exception,code,attempts", [
    (httpx.ConnectError, "connection_failed", 3),
    (httpx.ConnectTimeout, "backend_timeout", 3),
    (httpx.ReadTimeout, "backend_timeout", 3),
    (httpx.ReadError, "backend_unavailable", 3),
    (httpx.WriteError, "backend_unavailable", 3),
    (httpx.RemoteProtocolError, "backend_unavailable", 3),
    (httpx.LocalProtocolError, "configuration_error", 1),
    (httpx.UnsupportedProtocol, "configuration_error", 1),
    (httpx.ProxyError, "configuration_error", 1),
    (httpx.DecodingError, "invalid_backend_response", 1),
    (RuntimeError, "internal_error", 1),
])
async def test_transport_recovery(exception, code, attempts):
    """Classify exception types without disclosing exception messages."""
    calls = 0

    async def fail(request):
        """Raise a controlled failure at the transport boundary."""
        nonlocal calls
        calls += 1
        raise exception("PRIVATE-CONNECTION-DETAIL")

    backend = MockClient("http://mock", "key", {"user-001": "token"}, httpx.MockTransport(fail))
    with pytest.raises(ToolFailure, match=code) as caught:
        await backend.request("user-001", "GET", "/tickets")
    assert calls == attempts
    assert "PRIVATE" not in str(caught.value)


async def test_certificate_failure_is_not_retried():
    """Require configuration repair for TLS errors wrapped by a connection error."""
    async def fail(request):
        """Preserve the typed cause used by real TLS transports."""
        raise httpx.ConnectError("PRIVATE") from ssl.SSLCertVerificationError("PRIVATE")

    backend = MockClient("https://mock", "key", {"user-001": "token"}, httpx.MockTransport(fail))
    with pytest.raises(ToolFailure) as caught:
        await backend.request("user-001", "GET", "/access")
    assert caught.value.error.payload["code"] == "configuration_error"
    assert caught.value.error.attempts == 1


@pytest.mark.parametrize("value,expected", [(None, None), ("NaN", None), ("inf", None),
    ("-1", None), ("invalid", None), ("0", 0), ("0.5", 0.5),
    ("Wed, 21 Oct 2015 07:28:00 GMT", 0)])
def test_retry_after_parsing(value, expected):
    """Reject unusable backoff instructions and accept seconds and HTTP dates."""
    assert retry_after_seconds(value) == expected


@pytest.mark.parametrize("body", [b"not-json", b"null", b"[]", b"{}", b"x" * (MAX_RESPONSE_BYTES + 1)])
async def test_invalid_success_is_never_reported_as_success(body):
    """Treat malformed and oversized successful responses as permanent failures."""
    async def fail(request):
        """Return an HTTP success without a valid application result."""
        return httpx.Response(200, content=body)

    backend = MockClient("http://mock", "key", {"user-001": "token"}, httpx.MockTransport(fail))
    with pytest.raises(ToolFailure) as caught:
        await backend.request("user-001", "POST", "/tickets", session_id="session",
                              json={"title": "Keyboard", "description": "Broken", "approval_id": "same-id"})
    error = caught.value.error.payload
    assert error["code"] == "invalid_backend_response"
    assert error["outcome"] == "unknown"
    assert caught.value.error.attempts == 1


async def test_lost_write_then_denial_preserves_uncertainty():
    """A later denial cannot prove that the previous write did not commit."""
    calls = []

    async def fail(request):
        """Lose the first response, then simulate revoked access."""
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            raise httpx.ReadError("Lost response")
        return httpx.Response(403)

    backend = MockClient("http://mock", "key", {"user-001": "token"}, httpx.MockTransport(fail))
    with pytest.raises(ToolFailure) as caught:
        await backend.request("user-001", "POST", "/tickets", session_id="session",
                              json={"title": "Keyboard", "description": "Broken", "approval_id": "same-id"})
    error = caught.value.error.payload
    assert error["code"] == "access_denied"
    assert error["outcome"] == "unknown"
    assert len(calls) == 2 and calls[0] == calls[1]


async def test_cancellation_is_propagated_without_retry():
    """Respect caller cancellation instead of restarting an operation."""
    calls = 0

    async def cancel(request):
        """Cancel the current request at the transport boundary."""
        nonlocal calls
        calls += 1
        raise asyncio.CancelledError

    backend = MockClient("http://mock", "key", {"user-001": "token"}, httpx.MockTransport(cancel))
    with pytest.raises(asyncio.CancelledError):
        await backend.request("user-001", "GET", "/access")
    assert calls == 1


async def test_membership_and_data_share_deadline_and_request_id():
    """Membership must not reset the time budget available for the data call."""
    request_ids = []

    async def slow(request):
        """Spend part of the operation budget on each upstream call."""
        request_ids.append(request.headers["X-Request-Id"])
        await asyncio.sleep(0.04)
        return httpx.Response(200, json={"user_id": "user-001", "role": "employee"})

    backend = MockClient("http://mock", "key", {"user-001": "token"}, httpx.MockTransport(slow), deadline=0.065)
    with operation_context(backend.deadline):
        await backend.request("user-001", "GET", "/access")
        with pytest.raises(ToolFailure, match="deadline_exceeded"):
            await backend.request("user-001", "GET", "/access")
    assert len(request_ids) == 2 and len(set(request_ids)) == 1


async def test_no_membership_and_invalid_tool_inputs(server_options):
    """Reject discovery, execution, and invalid arguments without leaking values."""
    async with mcp_client(create_server(**server_options), auth="mcp-token-4") as mcp:
        with pytest.raises(McpError, match="access_denied"):
            await mcp.list_tools()
        with pytest.raises(ToolError, match="access_denied"):
            await mcp.call_tool("get_ticket", {"ticket_id": "TKT-001"})
    async with mcp_client(create_server(**server_options), auth="mcp-token-1") as mcp:
        with pytest.raises(ToolError, match="invalid_arguments") as caught:
            await mcp.call_tool("get_ticket", {"ticket_id": "PRIVATE-BAD-ID", "SECRET-KEY": "SECRET-VALUE"})
        assert "PRIVATE-BAD-ID" not in str(caught.value)
        assert "SECRET" not in str(caught.value)
        with pytest.raises(ToolError, match="unknown_tool"):
            await mcp.call_tool("approve_everything", {})
