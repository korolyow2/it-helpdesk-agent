"""Verify one argument correction without retrying access or service failures."""

import json

from langchain_core.messages import ToolMessage
import pytest

from agent_helpers import ScriptedModel, call, service
from helpdesk.errors import PublicError
from helpdesk.mock.store import TicketStore


@pytest.mark.parametrize("tool,bad,correct,method", [
    ("search_tickets", {"query": "VPN", "page": 0}, {"query": "VPN", "page": 1}, "search"),
    ("search_tickets", {"page": 1}, {"query": "VPN", "page": 1}, "search"),
    ("search_tickets", {"query": "VPN", "page": "1"}, {"query": "VPN", "page": 1}, "search"),
    ("search_tickets", {"query": "VPN", "PRIVATE-KEY": "PRIVATE-VALUE"}, {"query": "VPN"}, "search"),
    ("search_tickets", {"query": "x" * 501}, {"query": "VPN"}, "search"),
    ("get_ticket", {}, {"ticket_id": "TKT-001"}, "get_ticket"),
    ("get_ticket", {"ticket_id": "bad-id"}, {"ticket_id": "TKT-001"}, "get_ticket"),
])
async def test_invalid_args_are_corrected_once_before_mcp(running, tmp_path, monkeypatch, tool, bad, correct, method):
    """Use the discovered schema and keep invalid calls away from the backend."""
    executed = []
    original = getattr(TicketStore, method)

    def capture(self, *args):
        """Record operations that actually reach the mock store."""
        executed.append(args)
        return original(self, *args)

    monkeypatch.setattr(TicketStore, method, capture)
    model = ScriptedModel(messages=iter([
        call("discover_tools", {}, "discover-33"),
        call("list_tickets", {"page": 1, "page_size": 50}, "overview-33"),
        call(tool, bad),
        call(tool, correct, "corrected"),
        "Found an accessible ticket.",
    ]))
    async with service(running, tmp_path, model) as agent:
        result = await agent.run("user-001", "session", "Find my VPN ticket")
    messages = [item for item in model.seen[-1] if isinstance(item, ToolMessage) and item.name not in {"discover_tools", "list_tickets"}]
    error = json.loads(messages[0].text)
    assert set(error) == {"is_error", "code", "message", "is_retryable"}
    assert error["code"] == "invalid_arguments" and error["is_retryable"] is False
    assert "PRIVATE" not in messages[0].text
    assert [item["status"] for item in result["tool_calls"][2:]] == ["error", "success"]
    assert model.bindings[3] == [tool]
    assert len(executed) == 1


@pytest.mark.parametrize("second", [{"query": "VPN", "page": 0}, {"query": "VPN", "page": -1}])
async def test_second_invalid_call_ends_tool_use(running, tmp_path, monkeypatch, second):
    """Repeated and changed-but-invalid arguments both consume the single chance."""
    def forbidden(self, *args):
        """Fail if either locally invalid call reaches the mock."""
        raise AssertionError("Invalid arguments reached the mock")

    monkeypatch.setattr(TicketStore, "search", forbidden)
    model = ScriptedModel(messages=iter([
        call("discover_tools", {}, "discover-59"),
        call("list_tickets", {"page": 1, "page_size": 50}, "overview-55"),
        call("search_tickets", {"query": "VPN", "page": 0}),
        call("search_tickets", second, "second-invalid"),
        "I could not complete the search.",
    ]))
    async with service(running, tmp_path, model) as agent:
        result = await agent.run("user-001", "session", "Find a VPN ticket")
    assert [item["status"] for item in result["tool_calls"][2:]] == ["error", "error"]
    assert len(model.bindings) == 4


async def test_model_cannot_force_a_third_attempt(running, tmp_path):
    """Enforce the correction budget even if the model ignores disabled tools."""
    model = ScriptedModel(messages=iter([
        call("discover_tools", {}, "discover-73"),
        call("list_tickets", {"page": 1, "page_size": 50}, "overview-67"),
        call("search_tickets", {"query": "VPN", "page": 0}),
        call("search_tickets", {"query": "VPN", "page": -1}, "second-invalid"),
        call("search_tickets", {"query": "VPN", "page": 1}, "not-authorized"),
    ]))
    async with service(running, tmp_path, model) as agent:
        with pytest.raises(PublicError, match="execution_limit_exceeded"):
            await agent.run("user-001", "session", "Find a VPN ticket")


async def test_successful_repair_allows_normal_read_workflow(running, tmp_path):
    """A corrected search may be followed by reading its candidate within budgets."""
    model = ScriptedModel(messages=iter([
        call("discover_tools", {}, "discover-86"),
        call("list_tickets", {"page": 1, "page_size": 50}, "overview-79"),
        call("search_tickets", {"query": "VPN", "page": 0}),
        call("search_tickets", {"query": "VPN", "page": 1}, "corrected"),
        call("get_ticket", {"ticket_id": "TKT-001"}, "read-candidate"),
        "The ticket is open.",
    ]))
    async with service(running, tmp_path, model) as agent:
        result = await agent.run("user-001", "session", "Find and read a matching VPN ticket")
    assert [item["status"] for item in result["tool_calls"][2:]] == ["error", "success", "success"]
    assert set(model.bindings[4]) == {"list_tickets", "search_tickets", "get_ticket"}


async def test_new_user_turn_gets_its_own_correction_budget(running, tmp_path):
    """Persisted validation errors must not consume another message's chance."""
    model = ScriptedModel(messages=iter([
        call("discover_tools", {}, "discover-101"),
        call("list_tickets", {"page": 1, "page_size": 50}, "overview-92"),
        call("get_ticket", {}),
        call("get_ticket", {"ticket_id": "TKT-001"}, "correct-1"),
        "The ticket is open.",
        call("discover_tools", {}, "discover-105"),
        call("list_tickets", {"page": 1, "page_size": 50}, "overview-93"),
        call("get_ticket", {}, "invalid-2"),
        call("get_ticket", {"ticket_id": "TKT-002"}, "correct-2"),
        "Read the second ticket.",
    ]))
    async with service(running, tmp_path, model) as agent:
        first = await agent.run("user-001", "session", "Read the first ticket")
        second = await agent.run("user-001", "session", "Now read the second")
    assert [item["status"] for item in first["tool_calls"][2:]] == ["error", "success"]
    assert [item["status"] for item in second["tool_calls"][2:]] == ["error", "success"]


async def test_correction_cannot_switch_to_an_unrelated_tool(running, tmp_path):
    """The correction permission is scoped to the tool that rejected arguments."""
    model = ScriptedModel(messages=iter([
        call("discover_tools", {}, "discover-120"),
        call("list_tickets", {"page": 1, "page_size": 50}, "overview-105"),
        call("search_tickets", {"query": "VPN", "page": 0}),
        call("get_ticket", {"ticket_id": "TKT-001"}, "wrong-tool"),
    ]))
    async with service(running, tmp_path, model) as agent:
        with pytest.raises(PublicError, match="execution_limit_exceeded"):
            await agent.run("user-001", "session", "Find a VPN ticket")


async def test_backend_validation_can_also_be_corrected(running, tmp_path):
    """Domain validation remains authoritative when JSON Schema accepts whitespace."""
    model = ScriptedModel(messages=iter([
        call("discover_tools", {}, "discover-132"),
        call("list_tickets", {"page": 1, "page_size": 50}, "overview-116"),
        call("search_tickets", {"query": "   "}),
        call("search_tickets", {"query": "VPN"}, "corrected"),
        "Found VPN tickets.",
    ]))
    async with service(running, tmp_path, model) as agent:
        result = await agent.run("user-001", "session", "Find a VPN ticket")
    assert [item["status"] for item in result["tool_calls"][2:]] == ["error", "success"]


async def test_service_failure_does_not_grant_argument_repair(running, tmp_path, monkeypatch):
    """Do not use model correction to restart exhausted network retry budgets."""
    attempts = 0

    def unavailable(self, *args):
        """Simulate an upstream failure on an otherwise valid ticket read."""
        nonlocal attempts
        attempts += 1
        raise PublicError("backend_unavailable")

    monkeypatch.setattr(TicketStore, "get_ticket", unavailable)
    model = ScriptedModel(messages=iter([
        call("discover_tools", {}, "discover-154"),
        call("list_tickets", {"page": 1, "page_size": 50}, "overview-136"),
        call("get_ticket", {"ticket_id": "TKT-001"}),
        call("get_ticket", {"ticket_id": "TKT-002"}, "not-a-correction"),
    ]))
    async with service(running, tmp_path, model) as agent:
        with pytest.raises(PublicError, match="execution_limit_exceeded"):
            await agent.run("user-001", "session", "Read TKT-001")
    assert attempts == 3
