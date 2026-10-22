"""Verify protocol timeout translation without exposing private RPC messages."""

import json
from types import SimpleNamespace

from mcp import MCPError
from mcp.types import REQUEST_TIMEOUT
import pytest

from helpdesk.agent.tool_policy import mcp_transport_errors


@pytest.mark.parametrize("name,args,outcome", [
    ("get_ticket", {"ticket_id": "TKT-001"}, None),
    ("create_ticket", {"title": "Issue", "description": "Details"}, "not_applied"),
    ("create_ticket", {"title": "Issue", "description": "Details", "approval_id": "approved"}, "unknown"),
])
async def test_mcp_timeout_is_actionable_and_write_uncertainty_is_preserved(name, args, outcome):
    """A transport failure cannot become success or a falsely rolled-back write."""
    async def failing(request):
        """Raise the SDK's actual exception type with private diagnostic text."""
        raise MCPError(REQUEST_TIMEOUT, "PRIVATE-RPC-DETAIL")

    request = SimpleNamespace(tool_call={"name": name, "args": args, "id": "original-call"})
    result = await mcp_transport_errors.awrap_tool_call(request, failing)
    payload = json.loads(result.text)
    assert result.status == "error" and result.tool_call_id == "original-call"
    assert payload["code"] == "backend_timeout" and payload["is_retryable"] is True
    assert payload.get("outcome") == outcome
    assert "PRIVATE" not in result.text


@pytest.mark.parametrize("data", [{"error": "PRIVATE-RPC-DETAIL"}, {"error": {"code": []}}])
async def test_malformed_rpc_error_does_not_escape_or_echo_payload(data):
    """Treat invalid structured diagnostics as an unavailable backend."""
    async def failing(request):
        """Simulate an inconsistent remote JSON-RPC error."""
        raise MCPError(-32603, "PRIVATE-RPC-DETAIL", data)

    result = await mcp_transport_errors.awrap_tool_call(
        SimpleNamespace(tool_call={"name": "get_ticket", "args": {}, "id": "call"}), failing,
    )
    assert json.loads(result.text)["code"] == "backend_unavailable"
    assert "PRIVATE" not in result.text
