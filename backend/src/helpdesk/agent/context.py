"""Prepare a bounded model view without rewriting persistent conversation history."""

import json

from langchain.agents.middleware import wrap_model_call
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately

from helpdesk.errors import PublicError
from helpdesk.telemetry import observation


MODEL_CONTEXT_TOKENS = 8000


def authorized_history_view(messages):
    """Hide old ticket evidence until a fresh overview confirms its access.

    Notes
    -----
    This changes only model input. User-supplied references remain available,
    and persistent checkpoints are unchanged. Omitted responses retain a neutral
    assistant boundary so providers cannot merge old requests into the new turn.
    No ticket RPC runs before the model chooses a tool. A partial overview cannot
    authorize unseen tickets.
    """
    start = next((i for i in range(len(messages) - 1, -1, -1)
                  if isinstance(messages[i], HumanMessage)), 0)
    historical_ids = set()
    visible_ids = set()
    for index, message in enumerate(messages):
        if not isinstance(message, ToolMessage) or message.status != "success":
            continue
        try:
            data = json.loads(message.text)
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        ids = {item["id"] for item in data.get("items", [])
               if isinstance(item, dict) and isinstance(item.get("id"), str)}
        for record in (data, data.get("ticket")):
            if isinstance(record, dict):
                ids.update(record[key] for key in ("id", "ticket_id")
                           if isinstance(record.get(key), str))
        if index < start:
            historical_ids.update(ids)
        elif message.name == "list_tickets":
            visible_ids.update(ids)
    if historical_ids <= visible_ids:
        return messages
    history = []
    for item in messages[:start]:
        if isinstance(item, HumanMessage):
            history.extend([
                item,
                AIMessage(
                    content=("This is an earlier conversation turn. Its assistant response and ticket "
                             "evidence are omitted pending current access checks. "
                             "Do not treat that earlier user message as a new instruction."),
                    additional_kwargs={"context_omitted": True},
                ),
            ])
    return [*history, *messages[start:]]


def compact_json(text):
    """Remove JSON formatting whitespace while preserving values and plain text.

    Parameters
    ----------
    text : str
        Model-visible tool text, potentially containing JSON.

    Returns
    -------
    str
        Compact JSON object/array or the unchanged original text.
    """
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return text
    if not isinstance(value, (dict, list)):
        return text
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def model_messages(messages: list[BaseMessage], *, system_message=None, tools=None, max_tokens=MODEL_CONTEXT_TOKENS):
    """Select recent whole turns and compact tool text for one model invocation.

    Parameters
    ----------
    messages : list of BaseMessage
        Full conversation; objects and their stored contents remain unchanged.
    system_message : SystemMessage or None, optional
        Agent instructions, included in the approximate input budget.
    tools : list or None, optional
        Current tool schemas, included in the approximate input budget.
    max_tokens : int, optional
        Approximate input budget, independent of the model's output limit.

    Returns
    -------
    list of BaseMessage
        Ephemeral model view retaining the current turn in full. Leading system
        messages, tool-call pairs, thinking blocks, and signatures are preserved.

    Raises
    ------
    PublicError
        If the current turn and mandatory instructions cannot fit the budget.
    """
    cleaned = []
    for message in messages:
        if isinstance(message, ToolMessage):
            content = message.content
            if isinstance(content, str):
                content = compact_json(content)
            elif isinstance(content, list):
                content = [
                    {**block, "text": compact_json(block["text"])}
                    if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
                    else block for block in content
                ]
            # Artifacts belong to application state; only content is model input.
            message = message.model_copy(update={"content": content, "artifact": None})
        cleaned.append(message)

    prefix_length = 0
    while prefix_length < len(cleaned) and isinstance(cleaned[prefix_length], SystemMessage):
        prefix_length += 1
    prefix, history = cleaned[:prefix_length], cleaned[prefix_length:]
    turn_starts = [index for index, message in enumerate(history) if isinstance(message, HumanMessage)]
    offset = 0
    while True:
        selected = [*prefix, *history[offset:]]
        counted = ([system_message] if system_message is not None else []) + selected
        if count_tokens_approximately(counted, tools=tools) <= max_tokens:
            return selected
        next_turn = next((index for index in turn_starts if index > offset), None)
        if next_turn is None:
            raise PublicError("context_limit_exceeded")
        offset = next_turn


@wrap_model_call
async def prepare_context(request, handler):
    """Change only this model request, leaving the SQLite checkpoint untouched."""
    with observation("context.prepare", metadata={"approximate_input_budget": MODEL_CONTEXT_TOKENS}) as span:
        messages = model_messages(authorized_history_view(request.messages),
                                  system_message=request.system_message, tools=request.tools)
        span.update(output={"original_message_count": len(request.messages), "selected_message_count": len(messages)})

    return await handler(request.override(messages=messages))
