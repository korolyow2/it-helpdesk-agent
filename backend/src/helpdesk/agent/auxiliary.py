"""Bound refusal recovery for tool-free classification and evaluation calls."""

from langchain_anthropic import ChatAnthropic

from helpdesk.telemetry import observation


FALLBACK_MODEL = "claude-opus-4-8"
JUDGE_MODEL = "claude-opus-5-5"


async def invoke_auxiliary(model, messages, *, parent, name, schema=None):
    """Invoke an auxiliary model, falling back once after an Anthropic refusal.

    Parameters
    ----------
    model : BaseChatModel or Runnable
        Primary provider model, or an explicit test double.
    messages : list
        Transient provider input; never exported by this helper.
    parent : Observation
        Logical operation receiving attempt and serving-model diagnostics.
    name : str
        Stable generation name for individual provider invocations.
    schema : type or None, optional
        Pydantic output schema for the judge; classifiers use plain text.

    Returns
    -------
    tuple
        Unmodified last response and per-model reported usage. The caller still
        validates the decision or judgment. Provider errors propagate unchanged.
    """
    attempts = []
    models = [model]
    if isinstance(model, ChatAnthropic) and model.model != FALLBACK_MODEL:
        options = {"model": FALLBACK_MODEL}
        # Opus 4.8 does not accept Sonnet 5.5's tool-free classification mode.
        if (model.thinking or {}).get("type") == "between_tools":
            options["thinking"] = {"type": "disabled"}
        models.append(model.model_copy(update=options))
    parent.update(metadata={"fallback_model": FALLBACK_MODEL if len(models) > 1 else None})

    for index, active in enumerate(models):
        requested = getattr(active, "model", type(active).__name__)
        runner = (active.with_structured_output(schema, method="json_schema", include_raw=True)
                  if schema is not None and isinstance(active, ChatAnthropic) else active)
        parent.update(metadata={"fallback_attempted": index > 0, "attempt_count": index + 1})
        with observation(
            name, as_type="generation", model=requested,
            model_parameters={"max_tokens": getattr(active, "max_tokens", None),
                              "thinking": getattr(active, "thinking", None),
                              "effort": (getattr(active, "output_config", None) or {}).get("effort"),
                              "timeout": getattr(active, "default_request_timeout", None),
                              "max_retries": getattr(active, "max_retries", None)},
            metadata={"attempt_index": index + 1, "is_fallback": index > 0},
        ) as span:
            response = await runner.ainvoke(messages)
            raw = response["raw"] if isinstance(response, dict) else response
            stop = raw.response_metadata.get("stop_reason")
            served = raw.response_metadata.get("model_name") or raw.response_metadata.get("model") or requested
            usage = raw.usage_metadata
            category = (raw.response_metadata.get("stop_details") or {}).get("category")
            attempt = {"requested_model": requested, "response_model": served,
                       "is_fallback": index > 0, "stop_reason": stop, "reported_usage": usage,
                       "provider_response_id": raw.response_metadata.get("id"),
                       "refusal_category": category if category in {
                           "bio", "cyber", "frontier_llm", "reasoning_extraction", "general_harms",
                       } else None}
            attempts.append(attempt)
            span.update(model=served, output={"stop_reason": stop}, metadata=attempt)
            if usage:
                span.update(usage_details={"input": usage["input_tokens"], "output": usage["output_tokens"],
                                           "total": usage["total_tokens"]})
            if stop == "refusal":
                span.update(level="WARNING", status_message="provider_refused")

        parent.update(metadata={"attempts": attempts, "response_model": served,
                                "fallback_used": index > 0})
        if stop != "refusal" or index == len(models) - 1:
            return response, attempts
