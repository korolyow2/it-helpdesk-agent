"""Classify input before agent execution or persistent message storage."""

import asyncio
import hashlib
import json

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage

from helpdesk.agent.auxiliary import invoke_auxiliary
from helpdesk.agent.context import authorized_history_view
from helpdesk.agent.guardrails.prompts import BLOCKED_REPLY, GUARDRAIL_PROMPT
from helpdesk.errors import PublicError
from helpdesk.telemetry import observation


INTENTS = {"0": "helpdesk", "1": "blocked"}


class InputGuardrail:
    """Run an independent single-digit LLM classification without agent tools.

    Parameters
    ----------
    model : BaseChatModel
        Configured provider model; transport bounds are reused.
    """

    def __init__(self, model):
        self.model_name = getattr(model, "model", type(model).__name__)
        options = {"max_tokens": 1} if hasattr(model, "max_tokens") else {}
        if isinstance(model, ChatAnthropic):
            # Sonnet 5.5 replaces disabled thinking with this tool-free equivalent.
            thinking = "between_tools" if model.model.startswith("claude-sonnet-5-5") else "disabled"
            options.update(thinking={"type": thinking}, output_config={"effort": "low"})
        self.classifier = model.model_copy(update=options) if options else model

    async def classify(self, message, history):
        """Return a validated intent without persisting the candidate input.

        Parameters
        ----------
        message : str
            Current user input, used transiently for classification.
        history : list of BaseMessage
            User-scoped stored history; only recent conversational text is used.

        Returns
        -------
        str
            Validated routing decision, never permission or write authority.

        Raises
        ------
        PublicError
            If the classifier fails or does not return a complete valid decision.
        """
        safe_history = authorized_history_view([*history, HumanMessage(content=message)])[:-1]
        recent = [
            {"role": item.type, "content": item.text[-600:]}
            for item in safe_history if isinstance(item, (HumanMessage, AIMessage)) and item.text
        ][-6:]
        with observation(
            "guardrail.input", as_type="span",
            input={"message_characters": len(message), "context_messages": len(recent)},
            metadata={"policy_version": "input-v4-principles", "raw_input_recorded": False,
                      "requested_model": self.model_name,
                      "prompt_sha256": hashlib.sha256(GUARDRAIL_PROMPT.encode()).hexdigest(),
                      "max_tokens": getattr(self.classifier, "max_tokens", None)},
        ) as span:
            failure_type = "provider_error"
            try:
                raw, _ = await invoke_auxiliary(self.classifier, [
                    {"role": "system", "content": GUARDRAIL_PROMPT},
                    {"role": "user", "content": json.dumps({"recent_messages": recent, "message": message})},
                ], parent=span, name="guardrail.attempt")
                category = (raw.response_metadata.get("stop_details") or {}).get("category")
                span.update(metadata={
                    "stop_reason": raw.response_metadata.get("stop_reason"),
                    "refusal_category": category if category in {
                        "bio", "cyber", "frontier_llm", "reasoning_extraction", "general_harms",
                    } else None,
                })
                code = raw.text.strip()
                failure_type = "provider_refused" if raw.response_metadata.get("stop_reason") == "refusal" else "invalid_output"
                # One digit is complete even when the provider exhausts the one-token cap.
                if (code not in INTENTS or raw.tool_calls
                        or raw.response_metadata.get("stop_reason") == "refusal"):
                    raise ValueError("Input classification did not complete.")
                decision = "allow" if code == "0" else "block"
                span.update(output={"code": int(code), "intent": INTENTS[code], "decision": decision},
                            metadata={"input_intent": INTENTS[code], "guardrail_decision": decision})
                return INTENTS[code]
            except asyncio.CancelledError:
                span.update(output={"decision": "error", "error_code": "operation_interrupted"},
                            metadata={"failure_type": "interrupted"})
                raise
            except Exception:
                span.update(output={"decision": "error", "error_code": "guardrail_unavailable"},
                            metadata={"failure_type": failure_type})
                raise PublicError("guardrail_unavailable") from None
