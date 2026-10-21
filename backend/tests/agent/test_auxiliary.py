"""Verify refusal-only recovery, per-model telemetry and strict final validation."""

import json

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage
import pytest

from agent_helpers import ScriptedModel, service
from helpdesk.agent.auxiliary import FALLBACK_MODEL, JUDGE_MODEL
from helpdesk.agent.guardrails.input import BLOCKED_REPLY, InputGuardrail
from helpdesk.errors import PublicError
from helpdesk.evaluation import Judgment, judge_answer, judge_from_env


def provider_reply(text, model, *, stop="end_turn", input_tokens=10):
    """Build a provider-shaped response without contacting an API."""
    return AIMessage(content=text, response_metadata={"stop_reason": stop, "model_name": model},
                     usage_metadata={"input_tokens": input_tokens, "output_tokens": int(bool(text)),
                                     "total_tokens": input_tokens + int(bool(text))})


def primary_model():
    """Create a bounded client whose network method is replaced by each test."""
    return ChatAnthropic(model="claude-sonnet-5-5", api_key="synthetic-unit-test-key",
                         max_tokens=1024, thinking={"type": "adaptive"},
                         output_config={"effort": "medium"}, timeout=30, max_retries=2)


async def test_refusal_fallback_blocks_before_history_and_counts_usage_once(
    running, tmp_path, recorded_traces, monkeypatch,
):
    """Only a validated reserve decision may route a refused candidate."""
    seen = []

    async def invoke(model, messages, **kwargs):
        """Return a refusal followed by a single plain-digit reserve answer."""
        seen.append((model.model, model.max_tokens, model.thinking, model.default_request_timeout, model.max_retries))
        return (provider_reply("", model.model, stop="refusal") if len(seen) == 1
                else provider_reply("1", model.model, stop="max_tokens", input_tokens=20))

    monkeypatch.setattr(ChatAnthropic, "ainvoke", invoke)
    original = primary_model()
    client, exporter = recorded_traces
    async with service(running, tmp_path, ScriptedModel(messages=iter([])),
                       guardrail=InputGuardrail(original)) as agent:
        result = await agent.run("user-001", "session", "FALLBACK-INPUT-CANARY")
        assert result["reply"] == BLOCKED_REPLY and result["tool_calls"] == []
        assert [item async for item in agent.checkpointer.alist(None)] == []

    assert seen == [("claude-sonnet-5-5", 1, {"type": "between_tools"}, 30, 2),
                    (FALLBACK_MODEL, 1, {"type": "disabled"}, 30, 2)]
    assert original.model == "claude-sonnet-5-5" and original.max_tokens == 1024
    client.flush()
    spans = exporter.get_finished_spans()
    parent = next(span for span in spans if span.name == "guardrail.input")
    attempts = [span for span in spans if span.name == "guardrail.attempt"]
    assert len(attempts) == 2
    assert all(span.parent.span_id == parent.context.span_id for span in attempts)
    assert {span.attributes["langfuse.observation.model.name"] for span in attempts} == {
        "claude-sonnet-5-5", FALLBACK_MODEL,
    }
    assert sum(json.loads(span.attributes["langfuse.observation.usage_details"])["total"] for span in attempts) == 31
    assert "langfuse.observation.usage_details" not in parent.attributes
    assert "FALLBACK-INPUT-CANARY" not in json.dumps([dict(span.attributes) for span in spans])


@pytest.mark.parametrize("responses,expected_calls", [
    (["2"], 1),
    ([RuntimeError("PRIVATE-PROVIDER-DETAIL")], 1),
    (["refusal", "refusal"], 2),
    (["refusal", "2"], 2),
    (["refusal", RuntimeError("PRIVATE-PROVIDER-DETAIL")], 2),
])
async def test_invalid_outputs_and_errors_never_create_fabricated_decisions(monkeypatch, responses, expected_calls):
    """Do not switch models for invalid output or loop after reserve failure."""
    remaining = iter(responses)
    seen = []

    async def invoke(model, messages, **kwargs):
        """Provide the next explicit simulated outcome."""
        seen.append(model.model)
        value = next(remaining)
        if isinstance(value, Exception):
            raise value
        return provider_reply("" if value == "refusal" else value, model.model,
                              stop="refusal" if value == "refusal" else "end_turn")

    monkeypatch.setattr(ChatAnthropic, "ainvoke", invoke)
    with pytest.raises(PublicError, match="guardrail_unavailable"):
        await InputGuardrail(primary_model()).classify("Candidate", [])
    assert len(seen) == expected_calls
    assert seen[0] == "claude-sonnet-5-5"
    if expected_calls == 2:
        assert seen[1] == FALLBACK_MODEL


async def test_opus_judge_preserves_schema_and_both_attempts(monkeypatch):
    """Use the selected judge by default and retry the same schema after refusal."""
    monkeypatch.delenv("JUDGE_MODEL", raising=False)
    monkeypatch.setenv("MODEL_PROVIDER", "anthropic")
    monkeypatch.setattr("helpdesk.evaluation.model_from_env", primary_model)
    judge = judge_from_env()
    assert judge.model == JUDGE_MODEL == "claude-opus-5-5"
    assert judge.max_tokens == 2048 and judge.output_config == {"effort": "medium"}
    seen = []

    def structured(model, schema, **kwargs):
        """Keep provider-shaped structured responses without network calls."""
        assert schema is Judgment and kwargs == {"method": "json_schema", "include_raw": True}

        class Runner:
            """Return a refusal or a schema-validated reserve judgment."""

            async def ainvoke(self, messages):
                """Record the selected model and return its outcome."""
                seen.append(model.model)
                refused = model.model == JUDGE_MODEL
                return {"raw": provider_reply("" if refused else "{}", model.model,
                                              stop="refusal" if refused else "end_turn"),
                        "parsed": None if refused else Judgment(**{
                            key: True for key in ["groundedness", "helpfulness", "scope_adherence",
                                                  "instruction_adherence", "clarity"]
                        }, reason="Correct refusal."), "parsing_error": None}

        return Runner()

    monkeypatch.setattr(ChatAnthropic, "with_structured_output", structured)
    result = await judge_answer({"id": "example", "messages": ["Hello"], "expected": {}}, {}, judge)
    assert result["status"] == "completed" and seen == [JUDGE_MODEL, FALLBACK_MODEL]
    assert [item["response_model"] for item in result["attempts"]] == seen
