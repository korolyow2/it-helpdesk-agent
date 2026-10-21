"""Separate MCP authentication from live membership and backend availability."""

import asyncio
from contextlib import contextmanager
import re
from uuid import uuid4

from fastmcp.exceptions import (
    DisabledError, McpError, NotFoundError, ValidationError as ToolValidationError,
)
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.server.dependencies import get_access_token, get_http_headers
from fastmcp.server.middleware import Middleware
from pydantic import ValidationError

from helpdesk.errors import REQUEST_ID, PublicError
from helpdesk.mcp.errors import ToolFailure
from helpdesk.mcp.mock_client import OPERATION_DEADLINE
from helpdesk.telemetry import observation, remote_environment, remote_parent


def current_user():
    """Read the verified user subject from MCP authentication context.

    Returns
    -------
    str
        Authenticated user ID.

    Raises
    ------
    ToolFailure
        If no verified user context is available.
    """
    token = get_access_token()
    if token is None or not token.subject:
        raise ToolFailure(PublicError("authentication_required", attempts=0))
    return token.subject


def token_verifier(tokens):
    """Verify local MCP credentials without calling the ticket backend.

    Parameters
    ----------
    tokens : dict of str to str
        MCP-specific tokens mapped to user IDs.

    Returns
    -------
    StaticTokenVerifier
        Local credential verifier; membership is a separate middleware check.
    """
    return StaticTokenVerifier(tokens={
        token: {"client_id": "helpdesk-agent", "sub": user_id, "scopes": ["helpdesk"]}
        for token, user_id in tokens.items()
    })


@contextmanager
def operation_context(deadline, *, name="mcp.discovery", arguments=None, meta=None):
    """Share one backend time budget and correlation ID across membership and data.

    Parameters
    ----------
    deadline : float
        Maximum combined duration of backend work for the MCP operation.
    name : str, optional
        Stable MCP operation name.
    arguments : dict or None, optional
        Tool arguments without transport credentials.
    meta : dict or None, optional
        MCP per-call tracing metadata, independent of authorization.

    Yields
    ------
    Observation
        MCP span sharing correlation and the backend time budget.
    """
    budget = OPERATION_DEADLINE.set(asyncio.get_running_loop().time() + deadline)
    headers = get_http_headers()
    request_id = headers.get("x-request-id", "")
    if re.fullmatch(r"[0-9a-f-]{36}", request_id) is None:
        request_id = str(uuid4())
    correlation = REQUEST_ID.set(request_id)
    try:
        with observation(
            # The application header points to our exported parent; native MCP
            # metadata can point to a transport span intentionally not exported.
            name, trace_context=remote_parent(headers) or remote_parent(meta or {}), input=arguments,
            environment=remote_environment(headers),
            metadata={"request_id": request_id, "deadline_seconds": deadline},
        ) as span:
            yield span
    finally:
        REQUEST_ID.reset(correlation)
        OPERATION_DEADLINE.reset(budget)


class AccessMiddleware(Middleware):
    """Enforce membership at discovery and execution with distinct error actions.

    Parameters
    ----------
    backend : MockClient
        User-bound HTTP client for the mock permission source.
    """

    def __init__(self, backend):
        """Keep the permission source separate from token verification."""
        self.backend = backend

    async def on_list_tools(self, context, call_next):
        """Deny discovery on missing membership while preserving outage diagnostics."""
        with operation_context(self.backend.deadline) as span:
            try:
                await self.backend.request(current_user(), "GET", "/access")
                span.update(metadata={"access": "allowed"})
                return await call_next(context)
            except ToolFailure as error:
                raise McpError(code=-32000, message=str(error), data={"error": error.error.payload}) from None
            except Exception:
                error = PublicError("internal_error", attempts=0)
                raise McpError(code=-32603, message=str(error), data={"error": error.payload}) from None

    async def on_call_tool(self, context, call_next):
        """Return safe, actionable errors for permissions, invalid calls, and bugs."""
        name = context.message.name
        if name not in {"list_tickets", "search_tickets", "get_ticket", "create_ticket", "add_comment"}:
            name = "unknown"
        with operation_context(
            self.backend.deadline, name=f"mcp.{name}",
            arguments=context.message.arguments or {},
            meta=context.message.meta,
        ) as span:
            try:
                await self.backend.request(current_user(), "GET", "/access")
                result = await call_next(context)
                structured = getattr(result, "structured_content", None)
                span.update(output=structured, metadata={"access": "allowed"})
                return result
            except ToolFailure:
                raise
            except (ToolValidationError, ValidationError):
                raise ToolFailure(PublicError("invalid_arguments", attempts=0)) from None
            except (NotFoundError, DisabledError):
                raise ToolFailure(PublicError("unknown_tool", attempts=0)) from None
            except Exception:
                arguments = context.message.arguments or {}
                uncertain = context.message.name in {"create_ticket", "add_comment"} and bool(arguments.get("approval_id"))
                raise ToolFailure(PublicError("internal_error", attempts=0,
                                              outcome="unknown" if uncertain else None)) from None

