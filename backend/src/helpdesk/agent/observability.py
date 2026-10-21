"""Trace effective model inputs, streamed latency, and provider-reported usage."""

from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib

from langchain.agents.middleware import wrap_model_call
from langchain_core.callbacks import BaseCallbackHandler

from helpdesk.agent.prompts import SYSTEM_PROMPT
from helpdesk.telemetry import observation


ACTIVE_GENERATION = ContextVar("helpdesk_generation", default=None)


def message_data(messages):
    """Convert messages into role-labeled trace data, preserving tool correlations.

    Parameters
    ----------
    messages : iterable of BaseMessage
        Exact messages passed to or returned from the model.

    Returns
    -------
    list of dict
        Content and tool-call identifiers without private artifacts.
    """
    roles = {"human": "user", "ai": "assistant", "system": "system", "tool": "tool"}
    result = []
    for message in messages:
        item = {"role": roles.get(message.type, message.type), "content": message.content}
        if getattr(message, "tool_calls", None):
            item["tool_calls"] = message.tool_calls
        if getattr(message, "tool_call_id", None):
            item["tool_call_id"] = message.tool_call_id
        result.append(item)
    return result


class FirstTokenHandler(BaseCallbackHandler):
    """Measure the first nonempty output chunk without recording every token."""

    run_inline = True

    def on_llm_new_token(self, token, **kwargs):
        """Set generation completion start once, while streaming is in progress."""
        active = ACTIVE_GENERATION.get()
        if active is not None and not active[1] and token:
            active[0].update(completion_start_time=datetime.now(timezone.utc))
            active[1] = True


@wrap_model_call
async def trace_model(request, handler):
    """Trace a logical model call after context preparation.

    Parameters
    ----------
    request : ModelRequest
        Effective model context and available tools.
    handler : callable
        Next asynchronous model handler.

    Returns
    -------
    ModelResponse
        Unmodified model response.
    """
    model = request.model
    name = getattr(model, "model", None) or getattr(model, "model_name", type(model).__name__)
    parameters = {
        key: value for key, attribute in (
            ("max_tokens", "max_tokens"), ("timeout", "default_request_timeout"), ("max_retries", "max_retries"),
        )
        if isinstance(value := getattr(model, attribute, None), (int, float, str))
    }
    thinking = getattr(model, "thinking", None)
    if isinstance(thinking, dict):
        parameters["thinking_type"] = thinking.get("type")
    output_config = getattr(model, "output_config", None)
    if isinstance(output_config, dict):
        parameters["effort"] = output_config.get("effort")

    messages = ([request.system_message] if request.system_message is not None else []) + list(request.messages)
    with observation(
        "model.generate", as_type="generation", model=name,
        model_parameters=parameters, input=message_data(messages),
        metadata={
            "prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
            "tools": [{"name": tool.name, "description": tool.description,
                       "parameters": tool.args_schema} for tool in request.tools],
            "provider": model.__class__.__module__.split(".")[0],
        },
    ) as generation:
        token = ACTIVE_GENERATION.set([generation, False])
        try:
            response = await handler(request)
        finally:
            ACTIVE_GENERATION.reset(token)

        usage = {}
        for message in response.result:
            raw = message.usage_metadata or {}
            for key, normalized in (("input_tokens", "input"), ("output_tokens", "output"), ("total_tokens", "total")):
                if isinstance(raw.get(key), int):
                    usage[normalized] = usage.get(normalized, 0) + raw[key]
            for group, category in (("input_token_details", "input"), ("output_token_details", "output")):
                for key, value in (raw.get(group) or {}).items():
                    if isinstance(value, int):
                        normalized = f"{category}_{key}"
                        usage[normalized] = usage.get(normalized, 0) + value
                        # Match Langfuse's LangChain usage categories without double counting.
                        if category in usage and key != "priority" and not key.startswith("priority_"):
                            usage[category] = max(0, usage[category] - value)

        metadata = {
            "stop_reasons": [message.response_metadata.get("stop_reason") for message in response.result],
            "reported_usage": [message.usage_metadata for message in response.result],
            "provider_response_ids": [message.response_metadata.get("id") for message in response.result],
        }
        generation.update(output=message_data(response.result), metadata=metadata,
                          **({"usage_details": usage} if usage else {}))
        if "max_tokens" in metadata["stop_reasons"]:
            generation.update(level="ERROR", status_message="incomplete_model_output")
        return response
