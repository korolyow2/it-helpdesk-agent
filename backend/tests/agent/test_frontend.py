"""Verify the browser boundary without making paid provider calls."""

import httpx2 as httpx
import pytest

from agent_helpers import ScriptedModel, call, service
from helpdesk.api import create_app
from helpdesk.agent.workflow import build_workflow


def test_workflow_routes_screening_without_checkpointing():
    """Keep only native conditional routing around the existing ReAct graph."""
    graph = build_workflow(None, None)
    assert graph.checkpointer is False
    topology = graph.get_graph()
    assert set(topology.nodes) == {"__start__", "guardrail", "agent", "blocked", "__end__"}
    assert {(edge.source, edge.target) for edge in topology.edges} == {
        ("__start__", "guardrail"), ("guardrail", "agent"), ("guardrail", "blocked"),
        ("agent", "__end__"), ("blocked", "__end__"),
    }


async def test_local_frontend_and_pinned_component_are_served():
    """Serve the UI and local dependency without configuration secrets."""
    app = create_app(service=None, user_tokens={})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://chat") as client:
        response = await client.get("/")
        assert response.status_code == 200
        assert 'lang="en"' in response.text and "deep-chat-2.5.1.bundle.js" in response.text
        assert "mcp-token-" not in response.text and "ANTHROPIC_API_KEY" not in response.text
        assert (await client.get("/assets/vendor/deep-chat-2.5.1.bundle.js")).status_code == 200
        assert (await client.get("/assets/../.env")).status_code == 404


async def test_tool_details_are_separate_and_bound_to_the_authenticated_user(running, tmp_path):
    """Knowing a request ID cannot reveal another user's tool activity."""
    model = ScriptedModel(messages=iter([
        call("discover_tools", {}, "ui-discovery"), call("list_tickets", {}, "ui-overview"),
        call("get_ticket", {"ticket_id": "TKT-001"}, "ui-read"), "The ticket is open.",
    ]))
    async with service(running, tmp_path, model) as agent:
        app = create_app(service=agent, user_tokens={"mcp-token-1": "user-001", "mcp-token-2": "user-002"})
        result = await agent.run("user-001", "session", "Read TKT-001")
        path = f'/chat/requests/{result["request_id"]}/tools'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://chat") as client:
            own = await client.get(path, headers={"Authorization": "Bearer mcp-token-1"})
            assert own.status_code == 200
            assert own.json()["tools"] == [{"name": name, "status": "success"} for name in [
                "discover_tools", "list_tickets", "get_ticket",
            ]]
            assert "VPN disconnects" not in own.text and "arguments" not in own.text
            assert (await client.get(path, headers={"Authorization": "Bearer mcp-token-2"})).status_code == 404
            assert (await client.get(path)).status_code == 401


async def test_tool_summary_cache_is_bounded_and_cleared_after_restart(running, tmp_path):
    """Short-lived UI diagnostics are neither conversation memory nor authority."""
    async with service(running, tmp_path, ScriptedModel(messages=iter(["Hello."]))) as agent:
        for index in range(128):
            agent.completed_tools[f"old-{index}"] = ("user-001", [])
        result = await agent.run("user-001", "session", "Hello")
        assert len(agent.completed_tools) == 128
        assert "old-0" not in agent.completed_tools
        assert agent.request_tools("user-001", result["request_id"]) == []
    async with service(running, tmp_path, ScriptedModel(messages=iter([]))) as agent:
        from helpdesk.errors import PublicError

        with pytest.raises(PublicError, match="request_not_found"):
            agent.request_tools("user-001", result["request_id"])
