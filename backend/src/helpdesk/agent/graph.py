"""Configure the ready-made ReAct agent and bounded model execution."""

import os

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware, wrap_model_call
from langchain.chat_models import init_chat_model
from langchain_core.messages import SystemMessage

from helpdesk.agent.context import prepare_context
from helpdesk.agent.observability import trace_model
from helpdesk.agent.prompts import SYSTEM_PROMPT
from helpdesk.agent.tool_policy import overview_ready, tool_error_state, validate_tool_call
from helpdesk.errors import PublicError


@wrap_model_call
async def stop_tools_after_error(request, handler):
    """Offer one argument correction or disable tools after a terminal failure."""
    blocked, pending = tool_error_state(request.messages)
    if blocked:
        request = request.override(tools=[])
    elif pending is not None:
        request = request.override(tools=[tool for tool in request.tools if tool.name == pending.name])
    elif not overview_ready(request.messages):
        request = request.override(tools=[
            tool for tool in request.tools if tool.name in {"discover_tools", "list_tickets"}
        ])

    # Discovery results remain in context after their one-step gate expires.
    # Advertise the effective schemas so stale availability cannot imply denial.
    names = ", ".join(tool.name for tool in request.tools) or "none"
    context = (
        "<current_tools>\n"
        f"Tools available for this model call: {names}.\n"
        "This current list supersedes earlier discovery availability. "
        "Use an available detail tool to verify an explicitly identified ticket; "
        "absence from the overview is not a failed lookup. "
        "Do not claim to have attempted a tool unless its result is present.\n"
        "</current_tools>"
    )
    request = request.override(system_message=SystemMessage(content=[
        *request.system_message.content_blocks, {"type": "text", "text": context},
    ]))
    response = await handler(request)
    if any(item.response_metadata.get("stop_reason") == "max_tokens" for item in response.result):
        raise PublicError("incomplete_model_output")
    if any(item.response_metadata.get("stop_reason") == "refusal"
           and not item.text and not item.tool_calls for item in response.result):
        raise PublicError("model_refused")
    return response


def build_agent(model, tools, checkpointer, *, tool_middleware=None):
    """Create a ReAct agent using the supported LangChain factory.

    Parameters
    ----------
    model : BaseChatModel
        Configured model, or a scripted model for protocol tests.
    tools : list of BaseTool
        The permitted MCP read tools.
    checkpointer : AsyncSqliteSaver
        Persistent, user-scoped conversation checkpoint storage.
    tool_middleware : AgentMiddleware or None, optional
        Per-turn dynamic MCP discovery and tool resolution.

    Returns
    -------
    CompiledStateGraph
        Ready-made agent; no manually assembled graph nodes or edges.
    """
    return create_agent(
        model=model,
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        checkpointer=checkpointer,
        name="helpdesk",
        middleware=[
            ModelCallLimitMiddleware(run_limit=6, exit_behavior="error"),
            ToolCallLimitMiddleware(run_limit=6, exit_behavior="error"),
            *([tool_middleware] if tool_middleware is not None else []),
            stop_tools_after_error,
            prepare_context,
            trace_model,
            validate_tool_call,
        ],
    )


def model_from_env():
    """Configure Sonnet 5.5 with bounded adaptive thinking and provider overrides.

    Returns
    -------
    BaseChatModel
        Provider-configurable chat model with bounded API calls.

    Raises
    ------
    ValueError
        If MODEL_NAME is explicitly configured as empty.
    """
    provider = os.environ.get("MODEL_PROVIDER", "anthropic")
    name = os.environ.get("MODEL_NAME", "claude-sonnet-5-5").strip()
    if not name:
        raise ValueError("MODEL_NAME must not be empty.")
    options = {"max_tokens": 1024, "timeout": 30, "max_retries": 2}
    if provider == "anthropic":
        options.update(thinking={"type": "adaptive"}, output_config={"effort": "medium"})
        workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
        if workspace:
            options["default_headers"] = {"anthropic-workspace-id": workspace}
    return init_chat_model(model=name, model_provider=provider, **options)
