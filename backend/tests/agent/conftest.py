"""Expose the authenticated MCP HTTP stack to agent integration tests."""

from uuid import uuid4

from langfuse import Langfuse
from opentelemetry import context
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from fastmcp.utilities.tests import asgi_server
import httpx2 as httpx
import pytest

from helpdesk import telemetry
from helpdesk.mcp.server import create_server


@pytest.fixture
async def running(mock_app):
    """Expose the real authenticated MCP HTTP stack over an ASGI test transport."""
    server = create_server(
        mock_url="http://mock", service_key="test-service-key",
        mcp_user_tokens={f"mcp-token-{n}": f"user-{n:03}" for n in range(1, 5)},
        mock_user_credentials={f"user-{n:03}": f"test-token-{n}" for n in range(1, 5)},
        transport=httpx.ASGITransport(app=mock_app),
    )
    async with asgi_server(server, stateless_http=True) as http_server:
        yield http_server


@pytest.fixture
def recorded_traces(monkeypatch):
    """Export actual SDK spans into memory, including the export-stage mask."""
    exporter = InMemorySpanExporter()
    client = Langfuse(
        public_key=f"pk-lf-test-{uuid4().hex}", secret_key="sk-lf-synthetic-test",
        base_url="http://127.0.0.1:3000", tracer_provider=TracerProvider(),
        span_exporter=exporter, mask_otel_spans=telemetry.mask_export, environment="development",
    )
    monkeypatch.setattr(telemetry, "tracing_client", lambda: client)
    monkeypatch.setattr("helpdesk.agent.service.tracing_client", lambda: client)
    # SDK unit tests own their provider/exporter rather than the live pytest tree.
    token = context.attach(context.Context())
    try:
        yield client, exporter
    finally:
        context.detach(token)
        client.shutdown()

