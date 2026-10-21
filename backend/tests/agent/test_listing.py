"""Verify model-first orchestration and the overview dependency."""

from langchain.mcp import MCPAdapter
from langchain_core.messages import ToolMessage
import pytest

from agent_helpers import ScriptedModel, call, service
from helpdesk.errors import PublicError
from helpdesk.mcp.auth import AccessMiddleware
from helpdesk.mock.store import TicketStore


async def test_model_selects_overview_before_read(running, tmp_path, monkeypatch):
    """Ticket operations follow real model decisions, with no synthetic messages."""
    operations = []
    original = TicketStore.list_tickets
    original_discovery = AccessMiddleware.on_list_tools
    model = ScriptedModel(messages=iter([
        call("discover_tools", {}, "discover-15"),
        call("list_tickets", {}, "overview"),
        call("get_ticket", {"ticket_id": "TKT-001"}),
        "The ticket is open.",
    ]))

    def capture(self, *args):
        """Verify the first listing happens after the model has run."""
        assert len(model.seen) == 2
        operations.append("list_tickets")
        return original(self, *args)

    monkeypatch.setattr(TicketStore, "list_tickets", capture)

    async def capture_discovery(self, context, call_next):
        """Require a model decision before even schema discovery reaches MCP."""
        # The adapter can refresh output schemas after subsequent ticket calls.
        assert len(model.seen) >= 1
        if not operations:
            assert len(model.seen) == 1
        operations.append("discovery")
        return await original_discovery(self, context, call_next)

    monkeypatch.setattr(AccessMiddleware, "on_list_tools", capture_discovery)
    async with service(running, tmp_path, model) as agent:
        result = await agent.run("user-001", "session", "Read TKT-001")
    assert operations[0] == "discovery"
    assert operations.count("list_tickets") == 1
    assert not any(isinstance(item, ToolMessage) for item in model.seen[0])
    assert model.bindings[0] == ["discover_tools"]
    assert model.bindings[1] == ["list_tickets"]
    assert set(model.bindings[2]) == {"list_tickets", "search_tickets", "get_ticket"}
    assert [item["name"] for item in result["tool_calls"]] == ["discover_tools", "list_tickets", "get_ticket"]


async def test_out_of_scope_needs_no_ticket_calls(running, tmp_path, monkeypatch):
    """A refusal can complete without reading any ticket data."""
    def forbidden(self, *args):
        """Catch unnecessary overview calls."""
        raise AssertionError("Unexpected ticket read")

    monkeypatch.setattr(TicketStore, "list_tickets", forbidden)
    model = ScriptedModel(messages=iter([
        "I can help with IT Helpdesk requests.",
    ]))
    async with service(running, tmp_path, model) as agent:
        result = await agent.run("user-001", "session", "Write a birthday poem")
    assert result["tool_calls"] == []


@pytest.mark.parametrize("message,answer", [
    ("It is not working.", "Which application or service is affected?"),
    ("Thanks!", "You are welcome."),
    ("Write a birthday poem.", "I can help with IT Helpdesk requests."),
])
async def test_direct_response_never_connects_to_mcp(running, tmp_path, monkeypatch, message, answer):
    """No schema or membership request is needed for a model's direct response."""
    async def forbidden(*args, **kwargs):
        """Catch any eager adapter connection before the model decides."""
        raise AssertionError("Unexpected MCP connection")

    monkeypatch.setattr(MCPAdapter, "__aenter__", forbidden)
    model = ScriptedModel(messages=iter([answer]))
    async with service(running, tmp_path, model) as agent:
        result = await agent.run("user-001", "session", message)
    assert result["reply"] == answer
    assert result["tool_calls"] == []
    assert model.bindings == [["discover_tools"]]


async def test_new_turn_requires_new_discovery(running, tmp_path):
    """Earlier discovery cannot provide schemas or access on a later turn."""
    model = ScriptedModel(messages=iter([
        call("discover_tools", {}, "first-discovery"),
        call("list_tickets", {}, "first-overview"),
        "Here are the available tickets.",
        "Which application or service is affected?",
    ]))
    async with service(running, tmp_path, model) as agent:
        await agent.run("user-001", "session", "List tickets")
        result = await agent.run("user-001", "session", "Something else is broken")
    assert model.bindings[-1] == ["discover_tools"]
    assert result["tool_calls"] == []
    assert not any(isinstance(item, ToolMessage) for item in model.seen[-1])


async def test_unoffered_read_cannot_bypass_overview(running, tmp_path, monkeypatch):
    """Code also rejects a model that ignores the filtered tool list."""
    def forbidden(self, *args):
        """Catch any unauthorized direct detail execution."""
        raise AssertionError("Read bypassed overview")

    monkeypatch.setattr(TicketStore, "get_ticket", forbidden)
    model = ScriptedModel(messages=iter([
        call("get_ticket", {"ticket_id": "TKT-001"}),
        "The ticket tools must be discovered first.",
    ]))
    async with service(running, tmp_path, model) as agent:
        result = await agent.run("user-001", "session", "Read TKT-001")
    assert "unknown_tool" in result["tool_calls"][0]["content"]


async def test_failed_overview_returns_error_to_model(running, tmp_path, monkeypatch):
    """A failed listing disables tools while allowing an honest final answer."""
    def denied(self, *args):
        """Simulate access revocation after discovery."""
        raise PublicError("access_denied")

    monkeypatch.setattr(TicketStore, "list_tickets", denied)
    model = ScriptedModel(messages=iter([
        call("discover_tools", {}, "discover-74"),
        call("list_tickets", {}),
        "Ticket access was denied.",
    ]))
    async with service(running, tmp_path, model) as agent:
        result = await agent.run("user-001", "session", "Find tickets")
    assert result["tool_calls"][-1]["status"] == "error"
    assert len(model.seen) == 3
