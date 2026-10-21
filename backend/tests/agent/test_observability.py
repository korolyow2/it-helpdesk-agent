"""Inspect real Langfuse SDK exports without a cloud service or paid model."""

import json
from uuid import uuid4

from langfuse import propagate_attributes
from langchain_core.messages import AIMessage
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
import pytest

from agent_helpers import ScriptedModel, call, service
from helpdesk import telemetry
from helpdesk.errors import PublicError
from helpdesk.mock.store import TicketStore


async def test_trace_tree_inputs_usage_and_identity(running, tmp_path, recorded_traces):
    """Preserve one request tree with generation, MCP, and HTTP child spans."""
    client, exporter = recorded_traces
    answer = AIMessage(
        content="The ticket is open.", response_metadata={"stop_reason": "end_turn"},
        usage_metadata={"input_tokens": 120, "output_tokens": 8, "total_tokens": 128},
    )
    model = ScriptedModel(messages=iter([
        call("discover_tools", {}, "discover-40"),
        call("list_tickets", {}, "overview"),
        call("get_ticket", {"ticket_id": "TKT-001"}),
        answer,
    ]))
    async with service(running, tmp_path, model) as agent:
        result = await agent.run("user-001", "session", "Read the ticket")
    client.flush()
    spans = exporter.get_finished_spans()

    root = next(span for span in spans if span.name == "helpdesk.request")
    generations = [span for span in spans if span.name == "model.generate"]
    assert len(generations) == 4
    assert all(span.context.trace_id == root.context.trace_id for span in spans)
    first_tool = next(span for span in spans if span.name == "tool.execute")
    assert generations[0].end_time <= first_tool.start_time
    discovery = next(span for span in spans if span.name == "mcp.discovery")
    assert generations[0].end_time <= discovery.start_time
    assert root.parent is None
    assert root.attributes["user.id"] == "user-001"
    assert root.attributes["session.id"] != "session"
    assert json.loads(root.attributes["langfuse.observation.output"])["reply"] == result["reply"]
    usage = json.loads(generations[-1].attributes["langfuse.observation.usage_details"])
    assert usage["input"] == 120 and usage["output"] == 8
    assert "langfuse.observation.usage_details" not in generations[0].attributes
    assert "langfuse.observation.completion_start_time" in generations[-1].attributes
    assert "discover_tools" in json.dumps(dict(generations[0].attributes))
    assert '"tool"' not in generations[0].attributes["langfuse.observation.input"]
    assert {"mcp.list_tickets", "mcp.get_ticket", "backend.http_attempt"} <= {span.name for span in spans}
    by_id = {span.context.span_id: span for span in spans}
    assert all(span.parent.span_id in by_id for span in spans if span.parent is not None)

    evidence = [{"name": span.name, "trace_id": f"{span.context.trace_id:032x}",
                 "span_id": f"{span.context.span_id:016x}",
                 "parent_span_id": f"{span.parent.span_id:016x}" if span.parent else None,
                 "attributes": dict(span.attributes)} for span in spans]
    (tmp_path / "trace-evidence.json").write_text(json.dumps(evidence, indent=2))


async def test_retry_spans_explain_final_failure(running, tmp_path, monkeypatch, recorded_traces):
    """Expose every backend attempt and backoff without logging raw exceptions."""
    client, exporter = recorded_traces

    def unavailable(self, *args):
        """Fail each read through the supported safe error contract."""
        raise PublicError("backend_unavailable")

    monkeypatch.setattr(TicketStore, "get_ticket", unavailable)
    model = ScriptedModel(messages=iter([
        call("discover_tools", {}, "discover-86"),
        call("list_tickets", {}, "overview"),
        call("get_ticket", {"ticket_id": "TKT-001"}),
        "The ticket service could not complete the read.",
    ]))
    async with service(running, tmp_path, model) as agent:
        await agent.run("user-001", "session", "Read the ticket")
    client.flush()
    spans = exporter.get_finished_spans()
    attempts = [span for span in spans if span.name == "backend.http_attempt"
                and span.attributes.get("langfuse.observation.metadata.path") == '"/tickets/TKT-001"']
    waits = [span for span in spans if span.name == "backend.retry_wait"]
    assert len(attempts) == 3 and len(waits) == 2
    assert all("backend_unavailable" in json.dumps(dict(span.attributes)) for span in waits)
    failed_tool = next(span for span in spans if span.name == "tool.execute"
                       and span.attributes.get("langfuse.observation.metadata.tool_name") == '"get_ticket"')
    assert failed_tool.attributes["langfuse.observation.level"] == "ERROR"


async def test_terminal_failure_is_not_success(running, tmp_path, recorded_traces):
    """Mark the request as failed when a model ignores disabled tools."""
    client, exporter = recorded_traces
    model = ScriptedModel(messages=iter([
        call("discover_tools", {}, "discover-108"),
        call("list_tickets", {}, "overview"),
        call("get_ticket", {"ticket_id": "TKT-007"}),
        call("get_ticket", {"ticket_id": "TKT-007"}, "forbidden-retry"),
    ]))
    async with service(running, tmp_path, model) as agent:
        with pytest.raises(PublicError, match="execution_limit_exceeded"):
            await agent.run("user-001", "session", "Read the ticket")
    client.flush()
    root = next(span for span in exporter.get_finished_spans() if span.name == "helpdesk.request")
    assert root.attributes["langfuse.observation.level"] == "ERROR"
    assert "langfuse.observation.output" not in root.attributes


def test_export_redacts_secrets_and_raw_exception_text(recorded_traces, monkeypatch):
    """Exercise the SDK export mask, not only a sanitizer helper."""
    client, exporter = recorded_traces
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-configured-secret-916")
    with pytest.raises(RuntimeError):
        with telemetry.observation("safe.operation", input={
            "access_token": "unconfigured-secret", "message": "synthetic-configured-secret-916",
        }) as span:
            span.update(output="Bearer arbitrary-secret")
            raise RuntimeError("RAW-EXCEPTION-SECRET-771")
    client.flush()
    spans = exporter.get_finished_spans()
    text = json.dumps([dict(span.attributes) for span in spans])
    assert "[REDACTED]" in text
    assert all(value not in text for value in (
        "synthetic-configured-secret-916", "unconfigured-secret", "arbitrary-secret", "RAW-EXCEPTION-SECRET-771",
    ))
    assert all(not span.events for span in spans)


def test_tracing_cannot_default_to_cloud(monkeypatch):
    """Require explicit local project credentials before constructing an exporter."""
    telemetry.tracing_client.cache_clear()
    monkeypatch.setenv("HELPDESK_TRACING_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")
    with pytest.raises(ValueError, match="local Langfuse"):
        telemetry.tracing_client()
    telemetry.tracing_client.cache_clear()


def test_export_preserves_native_identifier_strings(recorded_traces):
    """Do not parse hexadecimal correlation IDs as JSON scientific notation."""
    client, exporter = recorded_traces
    with telemetry.observation("identifier.integrity"):
        span = trace.get_current_span()
        span.set_attribute("langfuse.experiment.id", "60e6580399529961")
        span.set_attribute("langfuse.experiment.item.root_observation_id", "1234567890123456")
    client.flush()
    exported = next(span for span in exporter.get_finished_spans() if span.name == "identifier.integrity")
    assert exported.attributes["langfuse.experiment.id"] == "60e6580399529961"
    assert exported.attributes["langfuse.experiment.item.root_observation_id"] == "1234567890123456"


async def test_user_session_ids_do_not_merge(running, tmp_path, recorded_traces):
    """Keep shared caller session strings distinct in the trace session view."""
    client, exporter = recorded_traces
    model = ScriptedModel(messages=iter([
        "First user.",
        "Second user.",
    ]))
    async with service(running, tmp_path, model) as agent:
        await agent.run("user-001", "shared-session", "Hello")
        await agent.run("user-002", "shared-session", "Hello")
    client.flush()
    roots = [span for span in exporter.get_finished_spans() if span.name == "helpdesk.request"]
    assert len({span.attributes["session.id"] for span in roots}) == 2
    assert len({span.context.trace_id for span in roots}) == 2


async def test_evaluation_environment_reaches_mcp_and_restores_default(running, tmp_path, recorded_traces):
    """Keep evaluation children together without changing later application traces."""
    client, exporter = recorded_traces
    model = ScriptedModel(messages=iter([
        call("discover_tools", {}, "environment-discovery"),
        call("list_tickets", {}, "environment-overview"),
        "Here are your accessible tickets.",
    ]))
    with propagate_attributes(environment="sdk-experiment", tags=["model-evaluation"]):
        with telemetry.observation("evaluation.example"):
            async with service(running, tmp_path, model) as agent:
                await agent.run("user-001", "session", "List tickets")
    with telemetry.observation("application.example"):
        pass
    client.flush()
    spans = exporter.get_finished_spans()
    application = next(span for span in spans if span.name == "application.example")
    evaluated = [span for span in spans if span is not application]
    assert {"mcp.discovery", "mock.http", "model.generate"} <= {span.name for span in evaluated}
    assert all(span.attributes["langfuse.environment"] == "sdk-experiment" for span in evaluated), [
        (span.name, span.attributes.get("langfuse.environment")) for span in evaluated
        if span.attributes.get("langfuse.environment") != "sdk-experiment"
    ]
    assert application.attributes.get("langfuse.environment", "development") == "development"


def test_trace_headers_forward_environment_without_identity(recorded_traces):
    """Do not forward user or session baggage alongside the display label."""
    with propagate_attributes(environment="protocol-tests", user_id="user-003",
                              session_id="private-session", as_baggage=True):
        with telemetry.observation("baggage.example"):
            headers = telemetry.trace_headers()
    assert set(headers) == {"traceparent", "baggage"}
    assert headers["baggage"] == "langfuse_environment=protocol-tests"
    assert telemetry.remote_environment(headers) == "protocol-tests"
    assert set(telemetry.remote_parent(headers)) == {"trace_id", "parent_span_id"}


def test_remote_environment_ignores_invalid_and_identity_baggage():
    """An incoming label cannot supply identity, roles, or arbitrary metadata."""
    for value in ("user_id=user-003,role=support", "langfuse_environment=INVALID",
                  "langfuse_environment=" + "x" * 41, "langfuse_environment=langfuse-internal"):
        assert telemetry.remote_environment({"baggage": value}) is None
    assert telemetry.remote_environment({"baggage": "role=support, langfuse_environment=sdk-experiment"}) == "sdk-experiment"


def test_filtered_framework_span_cannot_become_missing_parent(recorded_traces):
    """Attach app observations to the visible ancestor across hidden framework work."""
    client, exporter = recorded_traces
    native_provider = TracerProvider()
    try:
        with telemetry.observation("visible.parent"):
            with native_provider.get_tracer("fastapi").start_as_current_span("hidden.endpoint"):
                with telemetry.observation("visible.child"):
                    pass
        client.flush()
        spans = exporter.get_finished_spans()
        parent = next(span for span in spans if span.name == "visible.parent")
        child = next(span for span in spans if span.name == "visible.child")
        assert child.parent.span_id == parent.context.span_id
        assert child.context.trace_id == parent.context.trace_id
        assert not any(span.name == "hidden.endpoint" for span in spans)
    finally:
        native_provider.shutdown()
