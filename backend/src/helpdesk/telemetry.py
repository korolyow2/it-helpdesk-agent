"""Record bounded local Langfuse traces without leaking configured credentials."""

from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from functools import lru_cache
import json
import logging
import os
import re
from urllib.parse import urlparse

from langfuse import Langfuse, propagate_attributes
from langfuse.types import MaskOtelSpansResult, OtelSpanPatch
from opentelemetry import trace


logger = logging.getLogger(__name__)
SECRET_FIELD = re.compile(r"^(authorization|headers|.*password|.*secret.*|.*api_key|.*credential.*|.*service_key|.*token)$", re.I)
EXPORTED_PARENT = ContextVar("helpdesk_exported_parent", default=None)


def redact(data):
    """Redact credential fields and configured secret values, without detecting PII.

    Parameters
    ----------
    data : object
        JSON-compatible trace data.

    Returns
    -------
    object
        Sanitized copy; application data is never modified.
    """
    if isinstance(data, dict):
        return {
            key: "[REDACTED]" if SECRET_FIELD.fullmatch(str(key)) else redact(value)
            for key, value in data.items()
        }
    if isinstance(data, (list, tuple)):
        return [redact(value) for value in data if not (
            isinstance(value, dict) and value.get("type") in {"thinking", "redacted_thinking"}
        )]
    if not isinstance(data, str):
        return data

    for key, value in os.environ.items():
        if not any(word in key for word in ("SECRET", "KEY", "TOKEN", "PASSWORD")) or not value:
            continue
        secrets = [value]
        if key in {"MCP_USER_TOKENS", "MOCK_USER_TOKENS"}:
            try:
                secrets.extend(json.loads(value).keys())
            except (ValueError, AttributeError):
                pass
        for secret in secrets:
            if len(secret) >= 4:
                data = data.replace(secret, "[REDACTED]")

    return re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [REDACTED]", data)


def mask_export(*, params):
    """Mask final SDK span attributes immediately before Langfuse export.

    Parameters
    ----------
    params : MaskOtelSpansParams
        Immutable batch of SDK span snapshots.

    Returns
    -------
    MaskOtelSpansResult
        Sparse attribute patches with secrets removed.
    """
    patches = {}
    for identifier, span in params.spans.items():
        replacements = {}
        for key, value in span.attributes.items():
            if SECRET_FIELD.fullmatch(key.rsplit(".", 1)[-1]):
                replacements[key] = json.dumps("[REDACTED]")
                continue
            if not isinstance(value, str):
                continue
            try:
                # Native SDK identifiers can look like JSON numbers (e.g. a
                # hexadecimal ID containing only digits and 'e'). Parse only
                # structured payloads so correlation fields remain exact.
                clean = (json.dumps(redact(json.loads(value)), ensure_ascii=False)
                         if value.lstrip().startswith(("{", "[")) else redact(value))
            except (ValueError, TypeError):
                clean = redact(value)
            if clean != value:
                replacements[key] = clean
        if replacements:
            patches[identifier] = OtelSpanPatch(set_attributes=replacements)
    return MaskOtelSpansResult(span_patches=patches)


@lru_cache(maxsize=1)
def tracing_client():
    """Initialize an opt-in Langfuse client with an explicit local destination.

    Returns
    -------
    Langfuse or None
        Process-local client; disabled unless explicitly configured.

    Raises
    ------
    ValueError
        If enabled tracing lacks a local URL or project credentials.
    """
    if os.environ.get("HELPDESK_TRACING_ENABLED", "false").lower() != "true":
        return None

    base_url = os.environ.get("LANGFUSE_BASE_URL", "http://127.0.0.1:3000")
    target = urlparse(base_url)
    if (target.scheme not in {"http", "https"}
            or target.hostname not in {"localhost", "127.0.0.1", "::1", "langfuse-web"}
            or target.username or target.password or target.query or target.fragment
            or not os.environ.get("LANGFUSE_PUBLIC_KEY") or not os.environ.get("LANGFUSE_SECRET_KEY")):
        raise ValueError("Configure local Langfuse URL and project credentials before enabling tracing.")

    from fastmcp import settings

    # Our spans own the operation tree; keep native propagation without duplicate spans.
    settings.telemetry_mode = "propagation_only"
    return Langfuse(
        base_url=base_url, timeout=2, sample_rate=1.0,
        environment=os.environ.get("LANGFUSE_TRACING_ENVIRONMENT", "development"),
        release=os.environ.get("HELPDESK_RELEASE", "local"),
        mask_otel_spans=mask_export,
        should_export_span=lambda span: span.instrumentation_scope.name == "langfuse-sdk",
    )


class Observation:
    """Keep telemetry failures from changing a completed business operation."""

    def __init__(self, span=None):
        """Keep an optional SDK observation."""
        self.span = span

    def update(self, **kwargs):
        """Apply sanitized attributes when recording is available."""
        if self.span is not None:
            try:
                self.span.update(**redact(kwargs))
            except Exception:
                logger.warning("telemetry_update_failed")


@contextmanager
def observation(name, *, user_id=None, session_id=None, trace_context=None, environment=None, **kwargs):
    """Create a safe span and record errors without exporting exception text.

    Parameters
    ----------
    name : str
        Stable operation name without user-supplied values.
    user_id, session_id : str or None, optional
        Verified identity and user-scoped conversation key for a request root.
    trace_context : dict or None, optional
        Validated remote parent identifiers.
    environment : str or None, optional
        Validated telemetry label propagated across an HTTP boundary.
    **kwargs : dict
        SDK observation type, input, metadata, and generation settings.

    Yields
    ------
    Observation
        Fail-open telemetry update wrapper.
    """
    client = tracing_client()
    if client is None:
        yield Observation()
        return

    # Collapse native framework spans that our exporter intentionally filters.
    # Never reuse an ancestor from another trace or an isolated SDK test context.
    parent = EXPORTED_PARENT.get()
    ambient = trace.get_current_span().get_span_context()
    if trace_context is None and parent and parent["trace_id"] == f"{ambient.trace_id:032x}":
        trace_context = parent

    scope = propagate_attributes(environment=environment) if environment else nullcontext()
    with scope:
        manager = None
        propagation = nullcontext()
        try:
            manager = client.start_as_current_observation(
                name=name, trace_context=trace_context, **redact(kwargs),
            )
            span = manager.__enter__()
            if user_id is not None:
                propagation = propagate_attributes(
                    user_id=user_id, session_id=session_id, tags=["it-helpdesk"],
                )
            propagation.__enter__()
        except Exception:
            if manager is not None:
                try:
                    manager.__exit__(None, None, None)
                except Exception:
                    pass
            logger.warning("telemetry_start_failed")
            yield Observation()
            return

        current = Observation(span)
        active = trace.get_current_span().get_span_context()
        token = EXPORTED_PARENT.set({"trace_id": f"{active.trace_id:032x}",
                                     "parent_span_id": f"{active.span_id:016x}"})
        try:
            yield current
        except BaseException as error:
            public = getattr(error, "error", error)
            current.update(level="ERROR", status_message=str(getattr(public, "code", "operation_interrupted")))
            current.update(metadata={
                "exception_type": type(error).__name__,
                "http_status": getattr(error, "status_code", None),
                "provider_request_id": getattr(error, "request_id", None),
            })
            raise
        finally:
            # Never let automatic exception capture export raw errors or stack locals.
            propagation.__exit__(None, None, None)
            try:
                manager.__exit__(None, None, None)
            except Exception:
                logger.warning("telemetry_end_failed")
            EXPORTED_PARENT.reset(token)


def trace_headers():
    """Return W3C correlation and only the non-sensitive environment baggage.

    Returns
    -------
    dict
        Trace parent and optional environment; no identity or credentials.
    """
    span = trace.get_current_span()
    context = span.get_span_context()
    if not context.is_valid:
        return {}
    headers = {"traceparent": f"00-{context.trace_id:032x}-{context.span_id:016x}-01"}
    environment = getattr(span, "attributes", {}).get("langfuse.environment")
    if isinstance(environment, str) and re.fullmatch(r"(?!langfuse)[a-z0-9][a-z0-9_-]{0,39}", environment):
        headers["baggage"] = f"langfuse_environment={environment}"
    return headers


def remote_environment(headers):
    """Read only a valid Langfuse environment label from W3C baggage.

    Parameters
    ----------
    headers : mapping
        Incoming HTTP headers. Identity and arbitrary baggage are ignored.

    Returns
    -------
    str or None
        Display label only; never an authorization or user-memory source.
    """
    value = headers.get("baggage", "")
    if not isinstance(value, str):
        return None
    for entry in value.split(","):
        name, _, content = entry.partition("=")
        content = content.split(";", 1)[0].strip()
        if name.strip() == "langfuse_environment" and re.fullmatch(r"(?!langfuse)[a-z0-9][a-z0-9_-]{0,39}", content):
            return content
    return None


def remote_parent(headers):
    """Parse correlation identifiers without accepting baggage or authority.

    Parameters
    ----------
    headers : mapping
        HTTP headers from the authenticated MCP request.

    Returns
    -------
    dict or None
        SDK trace context or no parent for malformed headers.
    """
    value = headers.get("traceparent", "")
    if isinstance(value, str) and re.fullmatch(r"00-[0-9a-f]{32}-[0-9a-f]{16}-0[01]", value):
        _, trace_id, parent_span_id, _ = value.split("-")
        if int(trace_id, 16) and int(parent_span_id, 16):
            return {"trace_id": trace_id, "parent_span_id": parent_span_id}
    return None
