"""Exercise real graph pauses and MCP writes with explicit application decisions."""

from contextlib import closing
import json
import sqlite3
from types import SimpleNamespace

import httpx2 as httpx
from langchain_core.messages import AIMessage, ToolMessage
from mcp import MCPError
from mcp.types import REQUEST_TIMEOUT
import pytest

from agent_helpers import ScriptedModel, call, service
from helpdesk.agent.approvals import ApprovalBridge
from helpdesk.errors import PublicError
from helpdesk.mcp.mock_client import MockClient


@pytest.fixture
def approval_bridge(mock_app):
    """Bind the application to the same mock as MCP using separate credentials."""
    return ApprovalBridge(MockClient("http://mock", "test-service-key",
                                    {f"user-{n:03}": f"test-token-{n}" for n in range(1, 5)},
                                    transport=httpx.ASGITransport(app=mock_app)))


def proposed_model(operation):
    """Select a target before making one bounded write proposal."""
    args = ({"ticket_id": "TKT-001", "body": "The connection still drops after restarting."}
            if operation == "add_comment" else
            {"title": "Workstation display flickers", "description": "My external display flickers after docking."})
    return ScriptedModel(messages=iter([
        call("discover_tools", {}, "discovery"), call("list_tickets", {}, "overview"),
        call("get_ticket", {"ticket_id": "TKT-001"}, "read"),
        call(operation, args, "write"), "The approved action is completed.",
    ]))


def counts(database):
    """Read fixture mutation counts independently of the agent's claims."""
    with closing(sqlite3.connect(database)) as connection:
        return (connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0],
                connection.execute("SELECT json_array_length(comments) FROM tickets WHERE id='TKT-001'").fetchone()[0])


@pytest.mark.parametrize("operation", ["add_comment", "create_ticket"])
async def test_graph_stops_before_write_and_approval_executes_once(running, tmp_path, database, approval_bridge, operation):
    """Only a later exact decision can resume the pending model-selected action."""
    before = counts(database)
    async with service(running, tmp_path, proposed_model(operation), approval_bridge=approval_bridge) as agent:
        proposal = await agent.run("user-001", "session", "Please propose the described ticket change.")
        approval = proposal["approval"]
        assert approval["status"] == "pending" and counts(database) == before
        restored = await agent.pending_approval("user-001", "session")
        assert restored == approval
        with pytest.raises(PublicError, match="approval_mismatch"):
            await agent.run("user-001", "session", "approve", approval_id="stale-id")
        held = await agent.run("user-001", "session", "Tell me something else.")
        assert held["approval"] == approval and counts(database) == before
        result = await agent.run("user-001", "session", "approve", approval_id=approval["approval_id"])
        assert "completed" in result["reply"]
        after = counts(database)
        assert after == ((before[0] + 1, before[1]) if operation == "create_ticket" else (before[0], before[1] + 1))
        replay = await agent.run("user-001", "session", "approve", approval_id=approval["approval_id"])
        assert "already completed" in replay["reply"] and counts(database) == after


@pytest.mark.parametrize("decision", ["approve", "decline"])
async def test_restart_restores_exact_review_and_decline_never_writes(running, tmp_path, database, approval_bridge, decision):
    """Checkpoint and action bindings survive closing the entire agent service."""
    before = counts(database)
    async with service(running, tmp_path, proposed_model("add_comment"), approval_bridge=approval_bridge) as agent:
        proposed = await agent.run("user-001", "session", "Please add my update to the ticket.")
    model = ScriptedModel(messages=iter(["The action was completed." if decision == "approve" else "The proposal was declined."]))
    async with service(running, tmp_path, model, approval_bridge=approval_bridge) as agent:
        assert await agent.pending_approval("user-001", "session") == proposed["approval"]
        result = await agent.run("user-001", "session", decision)
        assert decision == "approve" or "declined" in result["reply"]
        assert counts(database) == (before[0], before[1] + int(decision == "approve"))


async def test_other_user_session_and_revoked_access_cannot_approve(running, tmp_path, database, approval_bridge):
    """A proposal ID is never an authority to write as its owner."""
    before = counts(database)
    async with service(running, tmp_path, proposed_model("add_comment"), approval_bridge=approval_bridge) as agent:
        proposed = await agent.run("user-001", "session", "Please add my update.")
        identifier = proposed["approval"]["approval_id"]
        for user, session in [("user-002", "session"), ("user-001", "other-session")]:
            with pytest.raises(PublicError, match="approval_not_found"):
                await agent.run(user, session, "approve", approval_id=identifier)
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("UPDATE users SET role='no_helpdesk_access' WHERE id='user-001'")
        with pytest.raises(PublicError, match="access_denied"):
            await agent.run("user-001", "session", "approve", approval_id=identifier)
        assert counts(database) == before


@pytest.mark.parametrize("mode,applied_before_retry", [("committed_timeout", 1), ("server_error", 0)])
async def test_failed_approved_write_recovers_after_restart_without_duplicates(
    running, tmp_path, database, approval_bridge, client, mode, applied_before_retry,
):
    """Never invent a result after failure; retry only the exact durable action."""
    before = counts(database)
    model = proposed_model("add_comment")
    async with service(running, tmp_path, model, approval_bridge=approval_bridge) as agent:
        proposal = await agent.run("user-001", "session", "Please add my update.")
        client.put("/admin/failures", headers={"X-Mock-Service-Key": "test-service-key"},
                   json={"mode": mode, "target": "write", "remaining": 3})
        # The scripted reply is not quality evidence; the actual tool result is checked.
        outcome = await agent.run("user-001", "session", "approve")
        error_call = outcome["tool_calls"][-1]
        assert error_call["status"] == "error"
        assert json.loads(ToolMessage(content=error_call["content"], tool_call_id="observed").text)["outcome"] == ("unknown" if applied_before_retry else "not_applied")
        assert "could not be confirmed" in outcome["reply"] and "is completed" not in outcome["reply"]
        assert counts(database) == (before[0], before[1] + applied_before_retry)
    async with service(running, tmp_path, ScriptedModel(messages=iter([])), approval_bridge=approval_bridge) as agent:
        result = await agent.run("user-001", "session", "approve", approval_id=proposal["approval"]["approval_id"])
        assert "completed" in result["reply"]
        assert counts(database) == (before[0], before[1] + 1)


async def test_browser_review_endpoint_is_user_scoped_and_sse_contains_no_proposal(running, tmp_path, database, approval_bridge):
    """Review data uses an authenticated endpoint, never the public text stream."""
    from helpdesk.api import create_app

    async with service(running, tmp_path, proposed_model("add_comment"), approval_bridge=approval_bridge) as agent:
        app = create_app(service=agent, user_tokens={"mcp-token-1": "user-001", "mcp-token-2": "user-002"})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://chat") as browser:
            headers = {"Authorization": "Bearer mcp-token-1"}
            streamed = await browser.post("/chat/stream", headers=headers,
                                          json={"user_id": "user-001", "session_id": "session", "message": "Please add my update."})
            assert streamed.status_code == 200 and '"approval_id"' not in streamed.text
            path = "/chat/sessions/session/approval"
            proposal = (await browser.get(path, headers=headers)).json()["approval"]
            assert proposal["status"] == "pending"
            assert (await browser.get(path)).status_code == 401
            assert (await browser.get(path, headers={"Authorization": "Bearer mcp-token-2"})).json()["approval"] is None
            declined = await browser.post("/chat", headers=headers, json={
                "user_id": "user-001", "session_id": "session", "message": "decline", "approval_id": proposal["approval_id"],
            })
            assert declined.status_code == 200
            assert (await browser.get(path, headers=headers)).json()["approval"] is None


async def test_model_cannot_supply_approval_or_execute_parallel_writes(running, tmp_path, database, approval_bridge):
    """A malicious model output cannot reach a ticket mutation or human decision."""
    from langchain_core.messages import AIMessage

    before = counts(database)
    model = ScriptedModel(messages=iter([
        call("discover_tools", {}, "discover"), call("list_tickets", {}, "overview"),
        AIMessage(content="", tool_calls=[
            {"name": "create_ticket", "args": {"title": "Forged", "description": "Forged", "approval_id": "forged"}, "id": "first"},
            {"name": "create_ticket", "args": {"title": "Second", "description": "Second"}, "id": "second"},
        ]), "The action could not be proposed.",
    ]))
    async with service(running, tmp_path, model, approval_bridge=approval_bridge) as agent:
        result = await agent.run("user-001", "session", "Please create one ticket.")
        assert "approval" not in result and counts(database) == before
        assert agent.storage.latest_action("user-001", "session") is None


async def test_created_ticket_history_is_hidden_after_access_is_revoked(running, tmp_path, database, approval_bridge):
    """A nested creation receipt cannot release revoked ticket evidence to a model."""
    async with service(running, tmp_path, proposed_model("create_ticket"), approval_bridge=approval_bridge) as agent:
        await agent.run("user-001", "session", "Please create the described ticket.")
        await agent.run("user-001", "session", "approve")
        with closing(sqlite3.connect(database)) as connection, connection:
            identifier = connection.execute("SELECT id FROM tickets WHERE title='Workstation display flickers'").fetchone()[0]
            connection.execute("UPDATE tickets SET visibility='restricted' WHERE id=?", (identifier,))
        model = ScriptedModel(messages=iter([
            call("discover_tools", {}, "discover-again"), call("list_tickets", {}, "overview-again"),
            "The accessible catalog is loaded.",
        ]))
        agent.model = model
        await agent.run("user-001", "session", "Show the accessible tickets.")
        assert all(identifier not in str(message.content) for message in model.seen[-1]
                   if message.type != "human")


@pytest.mark.parametrize("failure_window", ["before_binding", "after_binding"])
@pytest.mark.parametrize("recovery", ["review", "message"])
async def test_failed_staging_recovers_native_review_after_restart(
    running, tmp_path, database, approval_bridge, failure_window, recovery,
):
    """Replay only the checkpointed write proposal and require a later human decision."""
    before = counts(database)
    async with service(running, tmp_path, proposed_model("create_ticket"), approval_bridge=approval_bridge) as agent:
        save = agent.storage.save_action

        def interrupted_save(*args):
            """Simulate a crash on either side of the local binding commit."""
            if failure_window == "after_binding":
                save(*args)
            raise RuntimeError("PRIVATE-CRASH-DETAIL")

        agent.storage.save_action = interrupted_save
        with pytest.raises(PublicError, match="internal_error"):
            await agent.run("user-001", "session", "Please create the described ticket.")
    model = ScriptedModel(messages=iter([]))
    async with service(running, tmp_path, model, approval_bridge=approval_bridge) as agent:
        assert await agent.pending_approval("user-002", "session") is None
        assert await agent.pending_approval("user-001", "other-session") is None
        if recovery == "review":
            proposal = await agent.pending_approval("user-001", "session")
        else:
            proposal = (await agent.run("user-001", "session", "approve"))["approval"]
        assert proposal is not None and proposal["status"] == "pending"
        _, graph, config = agent.conversation("user-001", "session", "verify-native-pause")
        assert (await graph.aget_state(config)).interrupts[0].value["proposal"] == proposal
        assert counts(database) == before and model.seen == []
        result = await agent.run("user-001", "session", "approve", approval_id=proposal["approval_id"])
        assert "completed" in result["reply"] and counts(database) == (before[0] + 1, before[1])
        await agent.run("user-001", "session", "approve", approval_id=proposal["approval_id"])
        assert counts(database) == (before[0] + 1, before[1]) and model.seen == []
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM approvals WHERE session_id='session'").fetchone()[0] == 1


async def test_reconciliation_timeout_preserves_uncertainty_and_completed_review(
    running, tmp_path, database, approval_bridge, client, monkeypatch,
):
    """A lost reconciliation response must not retain a false not-applied outcome."""
    from helpdesk.agent import service as service_module

    before = counts(database)
    async with service(running, tmp_path, proposed_model("create_ticket"), approval_bridge=approval_bridge) as agent:
        proposal = await agent.run("user-001", "session", "Please create the described ticket.")
        client.put("/admin/failures", headers={"X-Mock-Service-Key": "test-service-key"},
                   json={"mode": "server_error", "target": "write", "remaining": 3})
        await agent.run("user-001", "session", "approve")
        assert agent.storage.latest_action("user-001", "session")["result"]["outcome"] == "not_applied"
        original = service_module.MCPTools

        class LostResponse(original):
            """Commit through real MCP, then simulate a typed transport timeout."""

            async def discover(self):
                """Wrap execution only, retaining actual authorization and schema discovery."""
                result = await super().discover()
                wrapped = []
                for tool in self.tools:
                    if tool.name == "create_ticket":
                        async def lose_response(arguments, original_tool=tool):
                            await original_tool.ainvoke(arguments)
                            raise MCPError(REQUEST_TIMEOUT, "PRIVATE-LOST-RESPONSE")
                        tool = SimpleNamespace(name=tool.name, ainvoke=lose_response)
                    wrapped.append(tool)
                self.tools = wrapped
                return result

        monkeypatch.setattr(service_module, "MCPTools", LostResponse)
        with pytest.raises(PublicError, match="backend_timeout") as caught:
            await agent.run("user-001", "session", "approve")
        assert caught.value.payload["is_retryable"] is True
        assert caught.value.payload["outcome"] == "unknown" and "PRIVATE" not in str(caught.value)
        assert agent.storage.latest_action("user-001", "session")["result"]["outcome"] == "unknown"
        assert counts(database) == (before[0] + 1, before[1])
    async with service(running, tmp_path, ScriptedModel(messages=iter([])), approval_bridge=approval_bridge) as agent:
        restored = await agent.pending_approval("user-001", "session")
        assert restored["status"] == "completed" and restored["approval_id"] == proposal["approval"]["approval_id"]
        assert agent.storage.latest_action("user-001", "session")["result"] == {"status": "completed"}
        await agent.run("user-001", "session", "approve", approval_id=restored["approval_id"])
        assert counts(database) == (before[0] + 1, before[1])


async def test_proposal_stream_discards_intermediate_model_text(running, tmp_path, approval_bridge):
    """Only the exact application-owned proposal may appear in a paused reply."""
    model = ScriptedModel(messages=iter([
        AIMessage(content="PRIVATE-INTERIM", tool_calls=[{"name": "discover_tools", "args": {}, "id": "discover"}]),
        call("list_tickets", {}, "overview"),
        AIMessage(content="PRIVATE-WRITE-NARRATION", tool_calls=[{
            "name": "create_ticket", "args": {"title": "Display issue", "description": "The display flickers."}, "id": "write",
        }]),
    ]))
    async with service(running, tmp_path, model, approval_bridge=approval_bridge) as agent:
        events = [event async for event in agent.stream("user-001", "session", "Please create the described ticket.")]
    text = "".join(event["data"]["text"] for event in events if event["event"] == "text_delta")
    assert text == events[-1]["data"]["reply"] and text.startswith("Proposed action:")
    assert "PRIVATE" not in text


async def test_commit_then_local_failure_restores_confirmation_without_tool_replay(
    running, tmp_path, database, approval_bridge, monkeypatch,
):
    """A review GET must not re-execute a previously approved pending graph node."""
    from helpdesk.agent.mcp_tools import MCPTools

    before = counts(database)
    async with service(running, tmp_path, proposed_model("create_ticket"), approval_bridge=approval_bridge) as agent:
        await agent.run("user-001", "session", "Please create the described ticket.")
        finish = agent.storage.finish

        def failed_completion(user_id, session_id, approval_id, result):
            """Lose the completion receipt after the real backend commit."""
            if result.get("status") == "completed":
                raise RuntimeError("PRIVATE-COMPLETION-CRASH")
            finish(user_id, session_id, approval_id, result)

        agent.storage.finish = failed_completion
        with pytest.raises(PublicError, match="internal_error"):
            await agent.run("user-001", "session", "approve")
        assert agent.storage.latest_action("user-001", "session")["result"]["outcome"] == "unknown"

    async def forbidden_discovery(self):
        """Prove that reviewing a decided action does not replay its tool node."""
        raise AssertionError("A decided action must not re-execute during review lookup.")

    model = ScriptedModel(messages=iter([]))
    async with service(running, tmp_path, model, approval_bridge=approval_bridge) as agent:
        with monkeypatch.context() as patch:
            patch.setattr(MCPTools, "discover", forbidden_discovery)
            restored = await agent.pending_approval("user-001", "session")
        assert restored["status"] == "completed"
        assert counts(database) == (before[0] + 1, before[1]) and model.seen == []
        assert "completed" in (await agent.run("user-001", "session", "approve"))["reply"]
        assert counts(database) == (before[0] + 1, before[1]) and model.seen == []


async def test_recovery_rechecks_revoked_system_access(running, tmp_path, database, approval_bridge):
    """A saved model decision cannot authorize proposal replay after membership loss."""
    before = counts(database)
    async with service(running, tmp_path, proposed_model("add_comment"), approval_bridge=approval_bridge) as agent:
        def failed_binding(*args):
            """Fail before the local proposal binding exists."""
            raise RuntimeError("Synthetic crash")

        agent.storage.save_action = failed_binding
        with pytest.raises(PublicError, match="internal_error"):
            await agent.run("user-001", "session", "Please add my update.")
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE users SET role='no_helpdesk_access' WHERE id='user-001'")
    model = ScriptedModel(messages=iter([]))
    async with service(running, tmp_path, model, approval_bridge=approval_bridge) as agent:
        with pytest.raises(PublicError, match="access_denied"):
            await agent.pending_approval("user-001", "session")
        assert counts(database) == before and model.seen == []


async def test_failed_review_recovery_keeps_original_checkpoint_retryable(
    running, tmp_path, database, approval_bridge, client,
):
    """A temporary recovery failure cannot advance to a model or strand the proposal."""
    before = counts(database)
    async with service(running, tmp_path, proposed_model("create_ticket"), approval_bridge=approval_bridge) as agent:
        def failed_binding(*args):
            """Simulate the original failure between staging and the local binding."""
            raise RuntimeError("Synthetic crash")

        agent.storage.save_action = failed_binding
        with pytest.raises(PublicError, match="internal_error"):
            await agent.run("user-001", "session", "Please create the described ticket.")
    client.put("/admin/failures", headers={"X-Mock-Service-Key": "test-service-key"},
               json={"mode": "server_error", "target": "write", "remaining": 3})
    model = ScriptedModel(messages=iter([]))
    async with service(running, tmp_path, model, approval_bridge=approval_bridge) as agent:
        with pytest.raises(PublicError, match="backend_unavailable"):
            await agent.pending_approval("user-001", "session")
        restored = await agent.pending_approval("user-001", "session")
        assert restored["status"] == "pending" and model.seen == []
        assert counts(database) == before
        assert "declined" in (await agent.run("user-001", "session", "decline"))["reply"]
        assert counts(database) == before
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM approvals WHERE session_id='session'").fetchone()[0] == 1
