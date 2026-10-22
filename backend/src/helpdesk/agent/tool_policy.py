"""Validate tool arguments and allow one bounded model-driven correction."""

import json

from jsonschema.validators import validator_for
from langchain.agents.middleware import wrap_tool_call
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from mcp import MCPError
from mcp.types import REQUEST_TIMEOUT

from helpdesk.errors import ERRORS, PublicError
from helpdesk.telemetry import observation


REPAIR_MARKER = "argument_repair"
WRITE_TOOLS = {"create_ticket", "add_comment"}


def tool_error_state(messages):
    """Find a fatal failure or the one argument error awaiting correction.

    Parameters
    ----------
    messages : list of BaseMessage
        Conversation messages; only the current user turn is considered.

    Returns
    -------
    tuple of bool and ToolMessage or None
        Whether further tools are blocked, and the pending correctable failure.
    """
    turn = []
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            break
        turn.append(message)

    errors = [
        message for message in turn
        if isinstance(message, ToolMessage) and message.status == "error"
    ]
    if not errors:
        return False, None
    if len(errors) != 1:
        return True, None

    error = errors[0]
    try:
        payload = json.loads(error.text)
    except (ValueError, TypeError):
        return True, None

    if not isinstance(payload, dict) or payload.get("code") != "invalid_arguments":
        return True, None
    if any(
        isinstance(message, ToolMessage)
        and message.additional_kwargs.get(REPAIR_MARKER) is True
        for message in turn
    ):
        return False, None

    return False, error


def current_turn(messages):
    """Return the current user turn without earlier tool outcomes."""
    start = next((i for i in range(len(messages) - 1, -1, -1)
                  if isinstance(messages[i], HumanMessage)), 0)
    return messages[start:]


def overview_ready(messages):
    """Require a successful model-selected overview in this user turn."""
    return any(isinstance(item, ToolMessage) and item.name == "list_tickets"
               and item.status == "success" for item in current_turn(messages))


def tool_error(call, code):
    """Return the minimal error without echoing invalid argument names or values.

    Parameters
    ----------
    call : dict
        Tool-call envelope produced by the model.
    code : str
        Registered safe error code.

    Returns
    -------
    ToolMessage
        Protocol-correlated failure visible to the model.
    """
    return ToolMessage(
        content=str(PublicError(code)),
        status="error",
        tool_call_id=call["id"],
        name=call["name"],
    )


async def _validate_tool_call(request, handler):
    """Check the discovered schema before MCP and bound argument correction.

    Parameters
    ----------
    request : ToolCallRequest
        Tool invocation and current agent state.
    handler : callable
        Next asynchronous tool handler.

    Returns
    -------
    ToolMessage
        Tool result or minimal argument validation error.

    Raises
    ------
    PublicError
        If execution is blocked or a correction violates its single-call limit.
    """
    messages = request.state["messages"]
    blocked, pending = tool_error_state(messages)
    if blocked:
        raise PublicError("execution_limit_exceeded")

    call = request.tool_call
    latest = next(message for message in reversed(messages) if isinstance(message, AIMessage))
    if len(latest.tool_calls) != 1 and any(item["name"] in WRITE_TOOLS for item in latest.tool_calls):
        return tool_error(call, "invalid_arguments")
    if pending is not None:
        latest = next(message for message in reversed(messages) if isinstance(message, AIMessage))
        if len(latest.tool_calls) != 1 or call["name"] != pending.name:
            raise PublicError("execution_limit_exceeded")

        original = next(
            original for message in messages if isinstance(message, AIMessage)
            for original in message.tool_calls if original["id"] == pending.tool_call_id
        )
        if call["args"] == original["args"]:
            return tool_error(call, "invalid_arguments")

    if request.tool is None:
        return tool_error(call, "unknown_tool")

    schema = request.tool.args_schema
    if not isinstance(schema, dict):
        schema = schema.model_json_schema()

    # The server uses strict inputs; unknown keys must not be silently ignored.
    schema = {**schema, "additionalProperties": False}
    if not validator_for(schema)(schema).is_valid(call["args"]):
        return tool_error(call, "invalid_arguments")

    if call["name"] not in {"discover_tools", "list_tickets"} and not overview_ready(messages):
        raise PublicError("execution_limit_exceeded")

    if call["name"] == "add_comment":
        reads = [item for item in current_turn(messages) if isinstance(item, ToolMessage)
                 and item.name == "get_ticket" and item.status == "success"]
        if not any(json.loads(item.text).get("id", "").upper() == call["args"]["ticket_id"].upper() for item in reads):
            return tool_error(call, "ticket_read_required")

    result = await handler(request)
    if pending is not None and isinstance(result, ToolMessage):
        result = result.model_copy(update={
            "additional_kwargs": {**result.additional_kwargs, REPAIR_MARKER: True},
        })

    return result


@wrap_tool_call
async def validate_tool_call(request, handler):
    """Trace schema validation, argument correction, and MCP execution.

    Parameters
    ----------
    request : ToolCallRequest
        Generated tool call and current agent state.
    handler : callable
        Next asynchronous handler.

    Returns
    -------
    ToolMessage
        Unmodified tool response with internal correction metadata.
    """
    with observation(
        "tool.execute", as_type="tool", input=request.tool_call["args"],
        metadata={"tool_name": request.tool_call["name"], "tool_call_id": request.tool_call["id"],
                  "argument_correction": tool_error_state(request.state["messages"])[1] is not None},
    ) as span:
        result = await _validate_tool_call(request, handler)
        span.update(output=result.content, metadata={"status": result.status})
        if result.status == "error":
            span.update(level="ERROR", status_message="tool_error")
        return result


@wrap_tool_call
async def mcp_transport_errors(request, handler):
    """Translate typed MCP transport failures before they escape the tool boundary."""
    return await invoke_mcp_safely(request.tool_call, lambda: handler(request))


async def invoke_mcp_safely(call, handler):
    """Preserve typed errors and write uncertainty for execution and reconciliation.

    Parameters
    ----------
    call : dict
        Trusted tool-call envelope, including application-injected approval ID.
    handler : callable
        Asynchronous zero-argument invocation of the resolved MCP tool.

    Returns
    -------
    ToolMessage
        Tool response or a sanitized, correlated transport failure.
    """
    try:
        return await handler()
    except (MCPError, TimeoutError) as error:
        data = getattr(error, "data", None)
        payload = data.get("error") if isinstance(data, dict) else None
        payload = payload if isinstance(payload, dict) else {}
        code = payload.get("code")
        if not isinstance(code, str) or code not in ERRORS:
            code = "backend_timeout" if isinstance(error, TimeoutError) or error.code == REQUEST_TIMEOUT else "backend_unavailable"
        outcome = None
        if call["name"] in WRITE_TOOLS:
            outcome = payload.get("outcome")
            if outcome not in ("unknown", "not_applied"):
                outcome = "unknown" if call["args"].get("approval_id") else "not_applied"
        failure = PublicError(code, outcome=outcome)
        with observation("mcp.transport_error", output=failure.payload,
                         metadata={"exception_type": type(error).__name__, "tool_name": call["name"]}):
            return ToolMessage(content=str(failure), status="error", tool_call_id=call["id"], name=call["name"])
