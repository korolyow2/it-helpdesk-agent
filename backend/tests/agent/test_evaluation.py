"""Verify evaluation integrity independently of paid model behavior."""

import json

import pytest
from langchain_core.messages import HumanMessage, ToolMessage, AIMessage
from pydantic import ValidationError

from helpdesk.agent.context import authorized_history_view
from helpdesk.evaluation import DATASET, Judgment, judge_answer, scores_for_result, summarize, tool_metrics
from helpdesk.evaluation import guardrail_metrics


def test_dataset_contracts_and_ids_are_explicit():
    """Keep the frozen suites reproducible and the held-out attacks visible."""
    cases = json.loads(DATASET.read_text())["cases"]
    assert len(cases) == len({case["id"] for case in cases})
    assert {case["suite"] for case in cases} == {"tool-calling", "answer-quality", "adversarial"}
    assert any(case.get("attack") and case.get("split") == "held_out" for case in cases)
    assert any(case["suite"] == "adversarial" and not case.get("attack") for case in cases)
    assert any(case.get("follow_up") for case in cases)
    assert any(case.get("injection") for case in cases)
    for case in cases:
        if case["expected"].get("no_tools"):
            assert case["required_calls"] == []
            assert case["allowed_tools"] == []


def test_tool_metrics_check_arguments_not_only_names():
    """Calling the right function with the wrong ticket must fail recall."""
    case = {"expected": {"read_ids": ["TKT-001"]},
            "required_calls": [{"name": "get_ticket", "arguments": {"ticket_id": "TKT-001"}}]}
    output = {"tool_calls": [{"name": "get_ticket", "arguments": {"ticket_id": "TKT-002"}}]}
    assert tool_metrics(case, output)["reference_call_recall"] == 0
    assert tool_metrics(case, output)["tool_precision"] == 0
    output["tool_calls"][0]["arguments"]["ticket_id"] = "tkt-001"
    assert tool_metrics(case, output)["reference_call_recall"] == 1
    assert tool_metrics(case, output)["tool_precision"] == 1


def test_missing_judge_is_not_a_pass_or_a_fabricated_score():
    """A failed evaluator cannot produce a successful joint verdict."""
    result = {"id": "example", "trial": 1, "status": "passed", "output": {}, "judge": {"status": "error"}}
    scores = {score.name: score.value for score in scores_for_result(result)}
    assert scores["judge_available"] is False
    assert scores["joint_pass"] is False
    assert "judge.groundedness" not in scores
    assert summarize([result])["joint_passes"] == 0


def test_binary_guardrail_metrics_keep_errors_and_wrong_routes_visible():
    """Missing decisions must not improve accuracy or hide false permissions."""
    metrics = guardrail_metrics([
        {"expected_guardrail_codes": [0, 1], "guardrail_codes": [1, 0]},
        {"expected_guardrail_codes": [1], "guardrail_codes": []},
        {"expected_guardrail_codes": [0], "guardrail_codes": [0]},
    ])
    assert metrics == {"expected_turns": 4, "classified_turns": 3, "correct": 1,
                       "false_blocks": 1, "false_allows": 1, "accuracy": 0.25}


def test_judge_output_rejects_missing_or_extra_dimensions():
    """Constrain evaluator output rather than accepting free-form approval."""
    with pytest.raises(ValidationError):
        Judgment.model_validate({"reason": "Everything is fine", "score": 1})


async def test_judge_failure_is_reported_without_raw_provider_exception():
    """Keep credential-bearing exception text outside results."""
    class FailingJudge:
        """Simulate a provider failure without an API call."""

        async def ainvoke(self, messages):
            """Reject the request with synthetic sensitive detail."""
            raise RuntimeError("PRIVATE-PROVIDER-DETAIL")

    result = await judge_answer({"id": "example", "messages": ["Hello"], "expected": {}}, {}, FailingJudge())
    assert result == {"status": "error", "error_type": "RuntimeError"}


def test_history_needs_fresh_positive_access_evidence():
    """Partial listings cannot release stored inaccessible ticket content."""
    history = [
        HumanMessage("Read TKT-007"),
        AIMessage("", tool_calls=[{"name": "get_ticket", "args": {"ticket_id": "TKT-007"}, "id": "old"}]),
        ToolMessage('{"id":"TKT-007","description":"PRIVATE-DETAIL"}', tool_call_id="old", name="get_ticket"),
        AIMessage("PRIVATE-DETAIL"), HumanMessage("Continue"),
    ]
    assert "PRIVATE-DETAIL" not in str(authorized_history_view(history))
    current = [
        AIMessage("", tool_calls=[{"name": "list_tickets", "args": {}, "id": "overview"}]),
        ToolMessage('{"items":[{"id":"TKT-001"}]}', tool_call_id="overview", name="list_tickets"),
    ]
    assert "PRIVATE-DETAIL" not in str(authorized_history_view(history + current))
    current[-1] = ToolMessage('{"items":[{"id":"TKT-007"}]}', tool_call_id="overview", name="list_tickets")
    assert "PRIVATE-DETAIL" in str(authorized_history_view(history + current))
    assert "PRIVATE-DETAIL" in str(history)


def test_effective_pagination_defaults_are_equivalent():
    """Do not reject a valid call merely because it relies on the MCP default."""
    case = {"expected": {}, "required_calls": [
        {"name": "search_tickets", "arguments": {"query": "VPN", "page": 1, "page_size": 2}},
    ]}
    output = {"tool_calls": [{"name": "search_tickets", "arguments": {"query": "VPN", "page_size": 2},
                              "status": "success", "content": '{"page":1,"page_size":2}'}]}
    assert tool_metrics(case, output)["reference_call_recall"] == 1
    output["tool_calls"][0]["arguments"]["page"] = 2
    assert tool_metrics(case, output)["reference_call_recall"] == 0


async def test_unparsed_judge_response_keeps_available_usage():
    """A provider refusal can consume tokens without producing a grade."""
    class RefusingJudge:
        """Return a real-shaped empty provider response without an API call."""

        async def ainvoke(self, messages):
            """Provide usage even though structured parsing failed."""
            return {"raw": AIMessage(content=[], response_metadata={"stop_reason": "refusal"},
                                     usage_metadata={"input_tokens": 10, "output_tokens": 0, "total_tokens": 10}),
                    "parsed": None, "parsing_error": ValueError("No structure")}

    result = await judge_answer({"id": "example", "messages": ["Hello"], "expected": {}}, {}, RefusingJudge())
    assert result["status"] == "error"
    assert result["stop_reason"] == "refusal"
    assert result["reported_usage"]["input_tokens"] == 10
