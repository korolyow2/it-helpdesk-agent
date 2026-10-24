"""Ensure graders reject false success instead of merely counting replies."""

import json
import pytest

from agent_helpers import AllowGuardrail, ScriptedModel, call
from helpdesk.evals import CATALOG, grade, model_trial


def test_read_grader_requires_observed_success_and_no_mutation():
    """A correct-looking answer without an actual read must fail."""
    case = {"expected": {"read_ids": ["TKT-001"], "answer_terms": ["open"]}}
    output = {"reply": "TKT-001 is open.", "ticket_count": 8, "approval_count": 0,
              "tool_calls": [{"name": "list_tickets", "status": "success", "content": '{"items":[]}'}]}
    assert grade(case, output)["read:TKT-001"] is False
    output["tool_calls"].append({"name": "get_ticket", "status": "success", "content": '{"id":"TKT-001"}'})
    assert all(grade(case, output).values())
    output["approval_count"] = 1
    assert grade(case, output)["no_mutation"] is False


def test_failure_grader_requires_error_evidence_and_exact_attempt_count():
    """A canned apology alone is not evidence of bounded failure handling."""
    case = {"expected": {"error_codes": ["backend_unavailable"], "fault_attempts": 3}}
    output = {"reply": "The read failed.", "ticket_count": 8, "approval_count": 0,
              "fault_attempts": 4, "tool_calls": []}
    checks = grade(case, output)
    assert checks["error:backend_unavailable"] is False
    assert checks["bounded_fault_attempts"] is False


def test_turn_grader_cannot_credit_an_old_lookup_for_a_new_browse_request():
    """Judge the current turn rather than accumulated calls from earlier work."""
    case = {"expected": {"turn_checks": [
        {"turn": 2, "required_tools": ["list_tickets"], "no_question": True,
         "forbidden_terms": ["account"]},
    ]}}
    output = {"reply": "Who is the account for?", "ticket_count": 8, "approval_count": 0,
              "tool_calls": [{"name": "list_tickets", "status": "success", "content": '{"items":[]}'}],
              "turns": [{"reply": "The catalog is loaded.", "tool_calls": [{"name": "list_tickets"}]},
                        {"reply": "Who is the account for?", "tool_calls": []}]}
    checks = grade(case, output)
    assert checks["turn:2:tool:list_tickets"] is False
    assert checks["turn:2:no_question"] is False
    assert checks["turn:2:absent:account"] is False
    output["turns"].pop()
    assert grade(case, output)["turn:2:present"] is False


def test_catalog_covers_assignment_and_tracks_implemented_features():
    """Map implemented memory and review requirements to executable checks."""
    cases = json.loads(CATALOG.read_text())["cases"]
    assert len({case["id"] for case in cases}) == len(cases)
    assert {"tool_selection", "approval", "scope", "failures", "memory", "permissions"} <= {
        case["category"] for case in cases
    }
    assert all(case["kind"] == "protocol" and case["selectors"]
               for case in cases if case["category"] == "memory")


@pytest.mark.parametrize("identifier", ["M01", "M11", "M15", "M16", "M17", "M18", "M20", "M21"])
async def test_model_trial_harness_with_scripted_decisions(identifier):
    """Verify the eval harness itself; scripted decisions do not measure model quality."""
    case = next(case for case in json.loads(CATALOG.read_text())["cases"] if case["id"] == identifier)
    if identifier == "M11":
        decisions = [call("discover_tools", {}), "You do not have Helpdesk access."]
    elif identifier == "M20":
        decisions = [call("get_ticket", {"ticket_id": "TKT-005"}), "The ticket is open.", "It is open."]
    elif identifier == "M21":
        decisions = [
            call("search_tickets", {"query": "VPN", "page": 1, "page_size": 2}),
            call("search_tickets", {"query": "VPN", "page": 2, "page_size": 2}, "page-2"),
            call("get_ticket", {"ticket_id": "TKT-004"}, "read"), "The incident is resolved.",
        ]
    else:
        answer = "The ticket service is unavailable; I could not read its status." if identifier == "M18" else "The ticket is open."
        decisions = [call("get_ticket", {"ticket_id": "TKT-001"}), answer]
    scripted = []
    beginning = True
    for item in decisions:
        if not isinstance(item, str) and beginning and identifier != "M11":
            scripted.append(call("discover_tools", {}, f"discovery-{len(scripted)}"))
            scripted.append(call("list_tickets", {}, f"overview-{len(scripted)}"))
            beginning = False
        scripted.append(item)
        if isinstance(item, str):
            beginning = True
    result = await model_trial(case, ScriptedModel(messages=iter(scripted)), guardrail=AllowGuardrail())
    assert result["status"] == "passed", result["checks"]
    assert result["output"]["ticket_count"] == 8


@pytest.mark.parametrize("identifier", ["M12", "G01"])
async def test_binary_block_and_recovery_evaluation_checks_actual_history(identifier):
    """Blocked turns remain outside SQLite even when a later valid turn succeeds."""
    from helpdesk.evaluation import DATASET

    class ClassifiedInput:
        """Inject deterministic routing only to verify the evaluation harness."""

        def __init__(self):
            self.intents = iter(["blocked", "helpdesk"])

        async def classify(self, message, history):
            """Return the next scripted decision without measuring a real model."""
            return next(self.intents)

    case = next(case for case in json.loads(DATASET.read_text())["cases"] if case["id"] == identifier)
    decisions = [] if identifier == "M12" else [
        call("discover_tools", {}), call("list_tickets", {}, "overview"),
        call("get_ticket", {"ticket_id": "TKT-001"}, "read"), "The ticket is open.",
    ]
    result = await model_trial(case, ScriptedModel(messages=iter(decisions)), guardrail=ClassifiedInput())
    assert result["status"] == "passed", result["checks"]
    assert result["output"]["turns"][0]["history_unchanged"] is True
    assert case["messages"][0] not in str(result["output"]["transcript"])
