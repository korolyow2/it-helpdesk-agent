"""Check create_agent and MCP integration without claiming real-model quality."""

from contextlib import closing
import json
import sqlite3

from langchain_core.messages import ToolMessage, AIMessage
import pytest

from helpdesk.errors import PublicError
from helpdesk.agent.prompts import SYSTEM_PROMPT
from agent_helpers import ScriptedModel, call, service


async def test_direct_read_through_factory_and_mcp(running, tmp_path):
    """A model tool decision reaches the mock through the real adapter and HTTP."""
    model = ScriptedModel(messages=iter([
        call("discover_tools", {}, "discover-16"),
        call("list_tickets", {"page": 1, "page_size": 50}, "overview-16"),
        call("get_ticket", {"ticket_id": "TKT-001"}),
        "The ticket is open.",
    ]))
    async with service(running, tmp_path, model) as agent:
        result = await agent.run("user-001", "session", "Read TKT-001")
    assert result["reply"] == "The ticket is open."
    assert len(result["tool_calls"]) == 3 and result["tool_calls"][0]["status"] == "success"
    tool_result = next(message for message in model.seen[-1] if isinstance(message, ToolMessage) and message.name == "get_ticket")
    assert "TKT-001" in tool_result.text and "VPN disconnects" in tool_result.text
    assert all(set(names) <= {"discover_tools", "list_tickets", "get_ticket", "search_tickets"} for names in model.bindings)


async def test_search_then_read(running, tmp_path):
    """Pass real search and ticket results back into the scripted model loop."""
    model = ScriptedModel(messages=iter([
        call("discover_tools", {}, "discover-32"),
        call("list_tickets", {"page": 1, "page_size": 50}, "overview-29"),
        call("search_tickets", {"query": "VPN", "page_size": 2}),
        call("get_ticket", {"ticket_id": "TKT-001"}, "call-2"),
        "The VPN ticket is open.",
    ]))
    async with service(running, tmp_path, model) as agent:
        result = await agent.run("user-001", "session", "My VPN is not working.")
    assert [item["name"] for item in result["tool_calls"]] == ["discover_tools", "list_tickets", "search_tickets", "get_ticket"]
    assert all(item["status"] == "success" for item in result["tool_calls"])


async def test_hidden_ticket_error_reaches_model_without_more_tools(running, tmp_path):
    """Preserve the minimal error payload and disable further tool use after failure."""
    model = ScriptedModel(messages=iter([
        call("discover_tools", {}, "discover-46"),
        call("list_tickets", {"page": 1, "page_size": 50}, "overview-40"),
        call("get_ticket", {"ticket_id": "TKT-007"}),
        "That ticket is unavailable.",
    ]))
    async with service(running, tmp_path, model) as agent:
        result = await agent.run("user-001", "session", "Read TKT-007")
    message = next(item for item in model.seen[-1] if isinstance(item, ToolMessage) and item.status == "error")
    error = json.loads(message.text)
    assert message.status == "error" and error["is_error"] is True
    assert set(error) == {"is_error", "code", "message", "is_retryable"}
    assert error["code"] == "not_found_or_inaccessible"
    assert "RESTRICTED-VPN-CANARY" not in str(model.seen)
    assert result["tool_calls"][-1]["status"] == "error"
    assert len(model.bindings) == 3
    assert "get_ticket" in model.bindings[2]
    assert model.bindings[0] == ["discover_tools"]
    assert model.bindings[1] == ["list_tickets"]
    assert all(messages[0].text == SYSTEM_PROMPT for messages in model.seen)


async def test_support_keeps_own_access(running, tmp_path):
    """The same tool uses the support user's credentials without elevated defaults."""
    model = ScriptedModel(messages=iter([
        call("discover_tools", {}, "discover-65"),
        call("list_tickets", {"page": 1, "page_size": 50}, "overview-55"),
        call("get_ticket", {"ticket_id": "TKT-007"}),
        "The investigation is in progress.",
    ]))
    async with service(running, tmp_path, model) as agent:
        result = await agent.run("user-003", "session", "Read TKT-007")
    assert result["tool_calls"][0]["status"] == "success"


async def test_no_helpdesk_membership_never_exposes_ticket_tools(running, tmp_path):
    """Let the model request discovery while live authorization denies ticket tools."""
    model = ScriptedModel(messages=iter([
        call("discover_tools", {}, "denied-discovery"),
        "You do not have Helpdesk access.",
    ]))
    async with service(running, tmp_path, model) as agent:
        result = await agent.run("user-004", "session", "Find tickets")
    assert result["tool_calls"][0]["status"] == "error"
    assert json.loads(result["tool_calls"][0]["content"])["code"] == "access_denied"
    assert model.bindings == [["discover_tools"]]
    assert len(model.seen) == 2


async def test_history_survives_restart_and_isolates_user_and_session(running, tmp_path):
    """A shared session ID cannot expose another user's persisted conversation."""
    first = ScriptedModel(messages=iter([
        "Acknowledged.",
    ]))
    async with service(running, tmp_path, first) as agent:
        await agent.run("user-001", "shared-session", "ALICE-PRIVATE-HISTORY-819")
    second = ScriptedModel(messages=iter([
        "Restored.",
        "Separate user.",
        "Separate session.",
    ]))
    async with service(running, tmp_path, second) as agent:
        await agent.run("user-001", "shared-session", "Continue")
        await agent.run("user-002", "shared-session", "Continue")
        await agent.run("user-001", "different-session", "Continue")
    assert "ALICE-PRIVATE-HISTORY-819" in str(second.seen[0])
    assert "ALICE-PRIVATE-HISTORY-819" not in str(second.seen[1:])
    assert "mcp-token" not in str(second.seen)


async def test_write_tool_cannot_execute_even_if_model_requests_it(running, tmp_path, database):
    """An invented write call must not reach any MCP write tool or stage approval."""
    model = ScriptedModel(messages=iter([
        call("discover_tools", {}, "discover-110"),
        call("list_tickets", {"page": 1, "page_size": 50}, "overview-89"),
        call("create_ticket", {"title": "Keyboard", "description": "Broken"}),
        "Writing is not available yet.",
    ]))
    async with service(running, tmp_path, model) as agent:
        await agent.run("user-001", "session", "Create a ticket")
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 8
        assert connection.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0


async def test_run_deadline_bounds_model_work(running, tmp_path):
    """Bound the entire run rather than only individual upstream tool calls."""
    import asyncio

    class SlowModel(ScriptedModel):
        """Simulate a model that never responds within the test budget."""

        async def _astream(self, messages, **kwargs):
            """Wait beyond the request deadline without contacting a provider."""
            await asyncio.sleep(1)
            for chunk in self._stream(messages, **kwargs):
                yield chunk

    model = SlowModel(messages=iter([
        "Too late",
    ]))
    async with service(running, tmp_path, model, deadline=0.08) as agent:
        with pytest.raises(PublicError, match="deadline_exceeded"):
            await agent.run("user-001", "session", "Find tickets")


async def test_model_cannot_repeat_tools_after_failure(running, tmp_path):
    """A model ignoring disabled tools cannot restart the MCP retry budget."""
    model = ScriptedModel(messages=iter([
        call("discover_tools", {}, "discover-145"),
        call("list_tickets", {"page": 1, "page_size": 50}, "overview-120"),
        call("get_ticket", {"ticket_id": "TKT-007"}),
        call("get_ticket", {"ticket_id": "TKT-007"}, "call-2"),
    ]))
    async with service(running, tmp_path, model) as agent:
        with pytest.raises(PublicError, match="execution_limit_exceeded"):
            await agent.run("user-001", "session", "Read TKT-007")
    assert len(model.seen) == 4


async def test_repeated_successful_calls_hit_model_budget(running, tmp_path):
    """Stop a looping model even when all requested reads succeed."""
    model = ScriptedModel(messages=iter([
        call("discover_tools", {}, "discover-158"),
        call("list_tickets", {}, "overview"),
        *[
        call("get_ticket", {"ticket_id": "TKT-001"}, f"call-{number}") for number in range(8)
    ],
    ]))
    async with service(running, tmp_path, model) as agent:
        with pytest.raises(PublicError, match="execution_limit_exceeded"):
            await agent.run("user-001", "session", "Read TKT-001")
    assert len(model.seen) == 6


async def test_parallel_calls_cannot_bypass_tool_budget(running, tmp_path):
    """Enforce a tool-call budget independently of the model-call budget."""
    batch = AIMessage(content="", tool_calls=[
        {"name": "get_ticket", "args": {"ticket_id": "TKT-001"}, "id": f"call-{n}"} for n in range(7)
    ])
    model = ScriptedModel(messages=iter([
        call("discover_tools", {}, "discover-175"),
        call("list_tickets", {}, "overview"),
        batch,
    ]))
    async with service(running, tmp_path, model) as agent:
        with pytest.raises(PublicError, match="execution_limit_exceeded"):
            await agent.run("user-001", "session", "Read tickets")
    assert len(model.seen) == 3


async def test_role_change_cannot_leak_stored_ticket_data(running, tmp_path, database):
    """Recheck historical ticket access before sending old tool data to the model."""
    first = ScriptedModel(messages=iter([
        call("discover_tools", {}, "discover-187"),
        call("list_tickets", {"page": 1, "page_size": 50}, "overview-154"),
        call("get_ticket", {"ticket_id": "TKT-007"}),
        "Read the restricted investigation.",
    ]))
    async with service(running, tmp_path, first) as agent:
        await agent.run("user-003", "session", "Read TKT-007")
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE users SET role = 'employee' WHERE id = 'user-003'")
    second = ScriptedModel(messages=iter([
        call("discover_tools", {}, "discover-196"),
        call("list_tickets", {}, "new-overview"),
        "That detail is unavailable.",
    ]))
    async with service(running, tmp_path, second) as agent:
        await agent.run("user-003", "session", "Repeat the previous details")
    assert "RESTRICTED-VPN-CANARY" not in str(second.seen)
    assert "Read the restricted investigation." not in str(second.seen)
    assert "TKT-007" in str(second.seen[0])  # The user's own reference is retained.



async def test_failed_tool_turn_can_be_followed_by_new_message(running, tmp_path, monkeypatch):
    """An interrupted read must not leave an invalid tool-call history forever."""
    import time
    from helpdesk.mock.store import TicketStore

    original = TicketStore.get_ticket

    def slow_read(self, user_id, ticket_id):
        """Delay a real read after the model has requested its tool."""
        time.sleep(1.0)
        return original(self, user_id, ticket_id)

    monkeypatch.setattr(TicketStore, "get_ticket", slow_read)
    first = ScriptedModel(messages=iter([
        call("discover_tools", {}, "discover-221"),
        call("list_tickets", {"page": 1, "page_size": 50}, "overview-180"),
        call("get_ticket", {"ticket_id": "TKT-001"}),
    ]))
    # Allow checkpoint persistence with tracing enabled, then time out the read.
    async with service(running, tmp_path, first, deadline=0.5) as agent:
        with pytest.raises(PublicError, match="deadline_exceeded"):
            await agent.run("user-001", "session", "Read TKT-001")
    assert len(first.seen) == 3
    monkeypatch.setattr(TicketStore, "get_ticket", original)
    second = ScriptedModel(messages=iter([
        "Please tell me which ticket to read.",
    ]))
    async with service(running, tmp_path, second) as agent:
        result = await agent.run("user-001", "session", "Let's continue")
        saved = [item async for item in agent.checkpointer.alist(None, limit=1)][0].checkpoint["channel_values"]["messages"]
    assert result["reply"] == "Please tell me which ticket to read."
    assert not any(isinstance(item, ToolMessage) for item in second.seen[0])
    answered = {item.tool_call_id for item in saved if isinstance(item, ToolMessage)}
    assert "call-1" in answered
    assert any(isinstance(item, ToolMessage) and item.status == "error" for item in saved)
