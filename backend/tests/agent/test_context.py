"""Verify non-destructive model input preparation and valid tool protocol groups."""

import json

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
import pytest

from agent_helpers import ScriptedModel, service
from helpdesk.agent.context import authorized_history_view, model_messages
from helpdesk.errors import PublicError


def test_hidden_history_retains_provider_turn_boundaries():
    """Separate past instructions from a new request without exposing old evidence."""
    from langchain_anthropic import ChatAnthropic

    messages = [
        HumanMessage("Hello, I need a new work account."),
        AIMessage(content="", tool_calls=[
            {"name": "get_ticket", "args": {"ticket_id": "TKT-007"}, "id": "old-read"},
        ]),
        ToolMessage('{"id":"TKT-007","description":"PRIVATE-EVIDENCE"}',
                    name="get_ticket", tool_call_id="old-read", status="success"),
        AIMessage("PRIVATE-EVIDENCE"),
        HumanMessage("Create all tickets."),
        AIMessage("Which issues should the tickets describe?"),
        HumanMessage("I want to see the available tickets."),
    ]
    original = [message.model_dump() for message in messages]
    view = authorized_history_view(messages)
    model = ChatAnthropic(model="claude-sonnet-5-5", api_key="test-key")
    payload = model._get_request_payload(view)
    assert [message["role"] for message in payload["messages"]] == [
        "user", "assistant", "user", "assistant", "user",
    ]
    assert view[-1] is messages[-1]
    assert payload["messages"][-1]["content"] == messages[-1].text
    assert "PRIVATE-EVIDENCE" not in str(payload)
    assert "TKT-007" not in str(payload)
    assert [message.model_dump() for message in messages] == original


def test_fresh_authorized_history_keeps_original_messages():
    """Restore original context only when current access checks cover its records."""
    messages = [
        HumanMessage("Read that ticket."),
        AIMessage(content="", tool_calls=[
            {"name": "get_ticket", "args": {"ticket_id": "TKT-001"}, "id": "old-read"},
        ]),
        ToolMessage('{"id":"TKT-001"}', name="get_ticket", tool_call_id="old-read", status="success"),
        AIMessage("The ticket is open."), HumanMessage("Show available tickets."),
        AIMessage(content="", tool_calls=[{"name": "list_tickets", "args": {}, "id": "fresh-list"}]),
        ToolMessage('{"items":[{"id":"TKT-001"}]}', name="list_tickets",
                    tool_call_id="fresh-list", status="success"),
    ]
    assert authorized_history_view(messages) is messages


def test_compact_tools_without_changing_persisted_objects():
    """Keep every tool value and correlation field while excluding app artifacts."""
    text = json.dumps({"id": "TKT-001", "description": "Keep  these spaces", "items": [1, 2]}, indent=4)
    original = ToolMessage(content=[{"type": "text", "text": text}], tool_call_id="call-1",
                           name="get_ticket", artifact={"internal": "PRIVATE-ARTIFACT"}, status="success")
    messages = [HumanMessage("Read TKT-001"),
                AIMessage(content="", tool_calls=[{"name": "get_ticket", "args": {"ticket_id": "TKT-001"}, "id": "call-1"}]),
                original]
    result = model_messages(messages)
    compact = result[-1]
    assert json.loads(compact.text) == json.loads(text)
    assert len(compact.text) < len(text)
    assert compact.tool_call_id == "call-1" and compact.name == "get_ticket" and compact.status == "success"
    assert compact.artifact is None
    assert original.text == text and original.artifact == {"internal": "PRIVATE-ARTIFACT"}


def test_drop_whole_old_turn_and_keep_current_tool_pairs():
    """Drop an oversized old exchange without orphaning current tool results."""
    system = SystemMessage("Keep the Helpdesk rules.")
    current = [HumanMessage("Read these tickets"),
               AIMessage(content="", tool_calls=[
                   {"name": "get_ticket", "args": {"ticket_id": f"TKT-00{n}"}, "id": f"call-{n}"} for n in (1, 2)
               ]),
               ToolMessage('{"id":"TKT-001"}', tool_call_id="call-1"),
               ToolMessage('{"id":"TKT-002"}', tool_call_id="call-2")]
    messages = [system, HumanMessage("OLD-REQUEST " + "x" * 8000), AIMessage("Old answer"), *current]
    prepared = model_messages(messages, max_tokens=500)
    assert prepared == [system, *current]
    assert len(messages) == 7


def test_provider_thinking_signatures_and_plain_text_are_preserved():
    """Do not strip required provider blocks or rewrite meaningful free text."""
    thinking = AIMessage(content=[{"type": "thinking", "thinking": "Internal block", "signature": "signature-819"}],
                         response_metadata={"model_provider": "anthropic", "output_version": "v1"})
    plain = ToolMessage("plain text {not valid JSON}", tool_call_id="call-1")
    result = model_messages([HumanMessage("Hello"), thinking, plain])
    assert result[1] is thinking
    assert result[1].content[0]["signature"] == "signature-819"
    assert result[2].content == plain.content


def test_large_current_turn_is_rejected_instead_of_partially_deleted():
    """Never silently drop the current question or its relevant tool response."""
    messages = [HumanMessage("Read TKT-001"),
                AIMessage(content="", tool_calls=[{"name": "get_ticket", "args": {"ticket_id": "TKT-001"}, "id": "call-1"}]),
                ToolMessage("x" * 8000, tool_call_id="call-1")]
    with pytest.raises(PublicError, match="context_limit_exceeded"):
        model_messages(messages, max_tokens=500)
    assert messages[-1].content == "x" * 8000


def test_instructions_and_tool_schemas_count_toward_input_budget():
    """Mandatory context cannot bypass the budget when user text is short."""
    tools = [{"type": "function", "function": {"name": "get_ticket", "description": "x" * 8000,
              "parameters": {"type": "object", "properties": {}}}}]
    with pytest.raises(PublicError, match="context_limit_exceeded"):
        model_messages([HumanMessage("Hello")], system_message=SystemMessage("Helpdesk rules"), tools=tools, max_tokens=500)


async def test_model_view_is_trimmed_but_sqlite_keeps_complete_history(running, tmp_path):
    """Exercise the actual middleware and checkpoint instead of only its helper."""
    model = ScriptedModel(messages=iter(["Acknowledged."] * 5))
    async with service(running, tmp_path, model) as agent:
        for number in range(4):
            await agent.run("user-001", "session", f"HISTORY-MARKER-{number} " + "x" * 9500)
        await agent.run("user-001", "session", "CURRENT-QUESTION")
        latest = [item async for item in agent.checkpointer.alist(None, limit=1)][0]
        saved = latest.checkpoint["channel_values"]["messages"]
    assert "HISTORY-MARKER-0" not in str(model.seen[-1])
    assert "HISTORY-MARKER-3" in str(model.seen[-1])
    assert "CURRENT-QUESTION" in str(model.seen[-1])
    assert "HISTORY-MARKER-0" in str(saved)
    assert "CURRENT-QUESTION" in str(saved)
    assert len(saved) == 10
