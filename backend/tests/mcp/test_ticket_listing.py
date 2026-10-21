"""Verify permission filtering and stable pagination of ticket overviews."""

import json

import pytest

from helpdesk.mcp.server import create_server
from test_server import mcp_client, server_options


@pytest.mark.parametrize("user,total", [(1, 6), (2, 6), (3, 8)])
async def test_overview_pagination_and_fields(server_options, user, total):
    """Count and page only authorized tickets, returning no descriptions."""
    async with mcp_client(create_server(**server_options), auth=f"mcp-token-{user}") as mcp:
        items = []
        for page in range(1, (total + 1) // 2 + 1):
            result = (await mcp.call_tool("list_tickets", {"page": page, "page_size": 2})).data
            assert result["total"] == total
            assert result["has_more"] == (page * 2 < total)
            items.extend(result["items"])

        empty = (await mcp.call_tool("list_tickets", {"page": 100, "page_size": 2})).data

    assert len(items) == total
    assert [item["id"] for item in items] == sorted({item["id"] for item in items})
    assert all(set(item) == {"id", "title", "status"} for item in items)
    assert empty["items"] == [] and empty["has_more"] is False
    if user != 3:
        assert "RESTRICTED" not in json.dumps(items)


@pytest.mark.parametrize("arguments", [{"page": 0}, {"page_size": 51}, {"query": "hidden"}])
async def test_overview_rejects_invalid_arguments(server_options, arguments):
    """Preserve strict MCP validation for page bounds and extra keys."""
    from fastmcp.exceptions import ToolError

    async with mcp_client(create_server(**server_options), auth="mcp-token-1") as mcp:
        with pytest.raises(ToolError, match="invalid_arguments"):
            await mcp.call_tool("list_tickets", arguments)
