"""Check progressive delivery, safe SSE errors, and Sonnet request compatibility."""

import asyncio
from contextlib import aclosing
import json

import httpx2 as httpx
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
import pytest

from agent_helpers import ScriptedModel, call, service
from helpdesk.agent.graph import model_from_env
from helpdesk.api import create_app
from helpdesk.errors import PublicError


class ProgressiveModel(ScriptedModel):
    """Produce separate thinking and text chunks without contacting a model API."""

    async def _astream(self, messages, **kwargs):
        """Yield a reasoning block followed by two observable text fragments."""
        self.seen.append(list(messages))
        yield ChatGenerationChunk(message=AIMessageChunk(content=[
            {"type": "thinking", "thinking": "PRIVATE-THINKING-819", "signature": "PRIVATE-SIGNATURE"},
        ]))
        yield ChatGenerationChunk(message=AIMessageChunk(content="Hello "))
        await asyncio.sleep(0.02)
        yield ChatGenerationChunk(message=AIMessageChunk(content="there."))


def events_from_sse(text):
    """Decode the event/data lines while ignoring optional SSE comments."""
    events = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        fields = dict(line.split(":", 1) for line in block.splitlines() if line.startswith(("event:", "data:")))
        if "event" in fields and "data" in fields:
            events.append({"event": fields["event"].strip(), "data": json.loads(fields["data"].strip())})
    return events


async def test_progressive_text_excludes_thinking(running, tmp_path):
    """Text must arrive before completion and must never contain thinking blocks."""
    model = ProgressiveModel(messages=iter([
    ]))
    async with service(running, tmp_path, model) as agent:
        async with aclosing(agent.stream("user-001", "session", "Hello")) as stream:
            first = await anext(stream)
            assert first["event"] == "text_delta" and first["data"]["text"] == "Hello "
            rest = [event async for event in stream]
    assert rest[0]["event"] == "text_delta" and rest[0]["data"]["text"] == "there."
    assert rest[-1]["event"] == "done" and rest[-1]["data"]["reply"] == "Hello there."
    assert "PRIVATE" not in json.dumps([first, *rest])


async def test_stream_close_releases_service(running, tmp_path):
    """A disconnected consumer must not leave the service lock or MCP client open."""
    model = ProgressiveModel(messages=iter([
    ]))
    async with service(running, tmp_path, model) as agent:
        stream = agent.stream("user-001", "session", "Hello")
        assert (await anext(stream))["event"] == "text_delta"
        await stream.aclose()
        assert not agent.lock.locked()
        assert (await agent.run("user-002", "session", "Hello"))["reply"] == "Hello there."


async def test_sse_endpoint_exposes_only_answer_text(running, tmp_path):
    """The HTTP stream excludes internal MCP calls, results, and thinking."""
    model = ScriptedModel(messages=iter([
        call("discover_tools", {}, "discover-70"),
        call("list_tickets", {}, "overview"),
        call("get_ticket", {"ticket_id": "TKT-001"}),
        "The ticket is open.",
    ]))
    async with service(running, tmp_path, model) as agent:
        app = create_app(service=agent, user_tokens={"mcp-token-1": "user-001"})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://chat") as client:
            response = await client.post("/chat/stream", headers={"Authorization": "Bearer mcp-token-1"},
                                         json={"user_id": "user-001", "session_id": "session", "message": "Read TKT-001"})
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/event-stream")
    events = events_from_sse(response.text)
    assert [item["event"] for item in events] == ["text_delta", "done"]
    assert events[0]["data"] == {"text": "The ticket is open."}
    assert events[-1]["data"]["reply"] == "The ticket is open."
    assert set(events[-1]["data"]) == {"reply", "request_id"}
    assert "tool_calls" not in response.text
    assert "VPN disconnects" not in response.text
    assert events[-1]["data"]["reply"] == "The ticket is open."


@pytest.mark.parametrize("token,user", [(None, "user-001"), ("invalid", "user-001"), ("mcp-token-1", "user-003")])
async def test_http_identity_cannot_be_chosen_in_body(running, tmp_path, token, user):
    """Reject missing credentials and user-ID spoofing before starting the stream."""
    model = ScriptedModel(messages=iter([
        "Must not execute",
    ]))
    async with service(running, tmp_path, model) as agent:
        app = create_app(service=agent, user_tokens={"mcp-token-1": "user-001"})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://chat") as client:
            response = await client.post("/chat/stream", headers={"Authorization": f"Bearer {token}"} if token else {},
                                         json={"user_id": user, "session_id": "session", "message": "Read tickets"})
    assert response.status_code == 401
    assert set(response.json()) == {"is_error", "code", "message", "is_retryable"}
    assert model.seen == []


async def test_sse_error_is_terminal_after_partial_text(running, tmp_path):
    """A partial answer followed by a failure must never become a completed reply."""
    class FailingModel(ProgressiveModel):
        """Fail after visible text with a diagnostic that must stay private."""

        async def _astream(self, messages, **kwargs):
            """Emit one fragment, then simulate a provider failure."""
            yield ChatGenerationChunk(message=AIMessageChunk(content="Partial reply"))
            raise RuntimeError("PRIVATE-PROVIDER-ERROR")

    async with service(running, tmp_path, FailingModel(messages=iter([
    ]))) as agent:
        app = create_app(service=agent, user_tokens={"mcp-token-1": "user-001"})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://chat") as client:
            response = await client.post("/chat/stream", headers={"Authorization": "Bearer mcp-token-1"},
                                         json={"user_id": "user-001", "session_id": "session", "message": "Hello"})
    events = events_from_sse(response.text)
    assert [item["event"] for item in events] == ["text_delta", "error"]
    assert set(events[-1]["data"]) == {"is_error", "code", "message", "is_retryable"}
    assert "PRIVATE" not in response.text


async def test_truncated_model_output_never_completes(running, tmp_path):
    """A provider output limit cannot turn a truncated answer into success."""
    model = ScriptedModel(messages=iter([
        AIMessage(content="Incomplete", response_metadata={"stop_reason": "max_tokens"}),
    ]))
    async with service(running, tmp_path, model) as agent:
        events = []
        with pytest.raises(PublicError, match="incomplete_model_output"):
            async for event in agent.stream("user-001", "session", "Hello"):
                events.append(event)
    assert not any(event["event"] == "done" for event in events)


def test_sonnet_request_uses_supported_thinking_parameters(monkeypatch):
    """Validate the real provider payload without making a network request."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-test-key")
    monkeypatch.delenv("MODEL_NAME", raising=False)
    monkeypatch.delenv("MODEL_PROVIDER", raising=False)
    model = model_from_env()
    payload = model._get_request_payload("Hello")
    assert payload["model"] == "claude-sonnet-5-5"
    assert payload["thinking"] == {"type": "adaptive"}
    assert payload["output_config"] == {"effort": "medium"}
    assert not {"temperature", "top_p", "top_k"} & payload.keys()
    assert payload["max_tokens"] == 1024


def test_workspace_selection_is_sent_as_a_header(monkeypatch):
    """Route multi-workspace credentials without adding workspace data to model input."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_test_workspace")
    monkeypatch.delenv("MODEL_NAME", raising=False)
    monkeypatch.delenv("MODEL_PROVIDER", raising=False)

    model = model_from_env()
    assert model._async_client.default_headers["anthropic-workspace-id"] == "wrkspc_test_workspace"
    assert "anthropic-workspace-id" not in model._get_request_payload("Hello")


async def test_http_sends_text_before_model_finishes(running, tmp_path):
    """Verify real HTTP chunk delivery without relying on a buffering ASGI client."""
    from fastmcp.utilities.asgi_transport import StreamingASGITransport

    release = asyncio.Event()

    class GatedModel(ScriptedModel):
        """Wait for the HTTP consumer before producing the end of the response."""

        async def _astream(self, messages, **kwargs):
            """Emit one fragment, then wait until the test has received it."""
            yield ChatGenerationChunk(message=AIMessageChunk(content="Hello "))
            await release.wait()
            yield ChatGenerationChunk(message=AIMessageChunk(content="there."))

    async with service(running, tmp_path, GatedModel(messages=iter([
    ]))) as agent:
        app = create_app(service=agent, user_tokens={"mcp-token-1": "user-001"})
        async with httpx.AsyncClient(transport=StreamingASGITransport(app), base_url="http://chat") as client:
            async with asyncio.timeout(2), client.stream(
                "POST", "/chat/stream", headers={"Authorization": "Bearer mcp-token-1"},
                json={"user_id": "user-001", "session_id": "session", "message": "Hello"},
            ) as response:
                lines = response.aiter_lines()
                while True:
                    line = await anext(lines)
                    if line.startswith("data:"):
                        assert json.loads(line.removeprefix("data:")) == {"text": "Hello "}
                        break
                assert not release.is_set()
                release.set()
                remaining = "\n".join([line async for line in lines])
    assert 'event: done' in remaining
    assert 'Hello there.' in remaining


async def test_no_helpdesk_access_is_explained_after_model_selected_discovery(running, tmp_path):
    """The model may explain denial without receiving any ticket tools or data."""
    model = ScriptedModel(messages=iter([
        call("discover_tools", {}),
        "You do not have Helpdesk access.",
    ]))
    async with service(running, tmp_path, model) as agent:
        app = create_app(service=agent, user_tokens={"mcp-token-4": "user-004"})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://chat") as client:
            response = await client.post("/chat/stream", headers={"Authorization": "Bearer mcp-token-4"},
                                         json={"user_id": "user-004", "session_id": "session", "message": "Read tickets"})
    events = events_from_sse(response.text)
    assert events[-1]["event"] == "done"
    assert "do not have Helpdesk access" in events[-1]["data"]["reply"]
    assert model.bindings == [["discover_tools"]]


async def test_nonstream_endpoint_and_safe_input_validation(running, tmp_path):
    """Keep the ordinary endpoint usable and never echo rejected fields."""
    model = ScriptedModel(messages=iter([
        "Hello.",
    ]))
    async with service(running, tmp_path, model) as agent:
        app = create_app(service=agent, user_tokens={"mcp-token-1": "user-001"})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://chat") as client:
            headers = {"Authorization": "Bearer mcp-token-1"}
            body = {"user_id": "user-001", "session_id": "session", "message": "Hello"}
            response = await client.post("/chat", headers=headers, json=body)
            rejected = await client.post("/chat/stream", headers=headers, json={**body, "SECRET-KEY": "PRIVATE-VALUE"})
    assert response.json()["reply"] == "Hello."
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "invalid_arguments"
    assert "SECRET" not in rejected.text and "PRIVATE" not in rejected.text


@pytest.mark.parametrize("status,attempts", [(503, 3), (401, 1)])
async def test_provider_retries_are_bounded_and_skip_auth_errors(monkeypatch, status, attempts):
    """Exercise the configured Anthropic SDK retry policy without network access."""
    import anthropic
    import langchain_anthropic.chat_models as anthropic_models

    calls = []

    async def fail(request):
        """Return a controlled provider failure from an in-memory HTTP transport."""
        calls.append(request)
        return httpx.Response(status, headers={"retry-after": "0"}, json={
            "type": "error", "error": {"type": "api_error", "message": "Synthetic provider failure"},
        })

    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-test-key")
    monkeypatch.delenv("MODEL_NAME", raising=False)
    monkeypatch.delenv("MODEL_PROVIDER", raising=False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        monkeypatch.setattr(anthropic_models, "_get_default_async_httpx_client", lambda **kwargs: client)
        model = model_from_env()
        with pytest.raises(anthropic.APIStatusError):
            await model.ainvoke("Hello")
        assert model._async_client.timeout == 30
    assert len(calls) == attempts


async def test_empty_provider_refusal_is_a_typed_failure(running, tmp_path):
    """Do not misreport a provider refusal as an internal bug or successful reply."""
    model = ScriptedModel(messages=iter([
        AIMessage(content=[], response_metadata={"stop_reason": "refusal"}),
    ]))
    async with service(running, tmp_path, model) as agent:
        with pytest.raises(PublicError, match="model_refused") as caught:
            await agent.run("user-001", "session", "A request declined by the provider")
    assert caught.value.payload["is_retryable"] is False
    assert len(model.seen) == 1
