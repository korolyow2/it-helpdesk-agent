"""Bridge explicit application decisions to backend approvals and graph pauses."""

import hashlib
import json
from contextvars import ContextVar
from urllib.parse import quote

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import interrupt

from helpdesk.agent.tool_policy import WRITE_TOOLS, current_turn, tool_error
from helpdesk.errors import PublicError
from helpdesk.mcp.errors import ToolFailure
from helpdesk.telemetry import observation


RECOVERING_REVIEW = ContextVar("helpdesk_recovering_review", default=False)


def decision_from_message(message):
    """Recognize unambiguous application commands without asking a model to approve."""
    return {"approve": "approve", "i approve": "approve", "decline": "decline", "i decline": "decline"}.get(
        message.strip().rstrip(".!").casefold()
    )


def proposal_reply(proposal):
    """Render exact proposed values as plain text for review, without claiming a write."""
    arguments = "\n".join(f"{name}: {value}" for name, value in proposal["arguments"].items())
    return (f"Proposed action: {proposal['operation']}\n{arguments}\n"
            "No change has been made. Reply approve or decline, or use the review buttons.")


class ApprovalBridge:
    """Use separate mock credentials only for application-owned approval endpoints.

    Parameters
    ----------
    client : MockClient
        Bounded client; ticket data and mutations remain behind MCP.
    """

    def __init__(self, client):
        self.client = client

    async def get(self, user_id, session_id, approval_id):
        """Restore one approval while rechecking current backend authorization."""
        try:
            return await self.client.request(user_id, "GET", f"/approvals/{quote(approval_id, safe='')}", session_id=session_id)
        except ToolFailure as error:
            raise error.error from None

    async def decide(self, user_id, session_id, proposal, decision):
        """Verify the exact action before recording an explicit human decision."""
        with observation("approval.review", input={"approval_id": proposal["approval_id"], "decision": decision}) as span:
            current = await self.get(user_id, session_id, proposal["approval_id"])
            if any(current[key] != proposal[key] for key in ("operation", "arguments", "approval_id")):
                raise PublicError("approval_mismatch")
            if current["status"] == "completed" and decision == "approve":
                return current
            try:
                result = await self.client.request(
                    user_id, "POST", f"/approvals/{quote(proposal['approval_id'], safe='')}/decision",
                    session_id=session_id, json={"decision": decision},
                )
            except ToolFailure as error:
                raise error.error from None
            span.update(output={"status": result["status"]})
            return result


class WriteReview(AgentMiddleware):
    """Stage an exact MCP action, pause durably and execute only recorded approval.

    Parameters
    ----------
    storage : AppStorage
        Durable call-to-proposal bindings, including replayed tool-node calls.
    user_id, session_id : str
        Trusted application identity and conversation scope.
    """

    def __init__(self, storage, user_id, session_id):
        self.storage, self.user_id, self.session_id = storage, user_id, session_id

    async def awrap_tool_call(self, request, handler):
        """Keep the graph interrupted until a subsequent verified user decision."""
        call = request.tool_call
        if call["name"] not in WRITE_TOOLS:
            return await handler(request)
        latest = next(item for item in reversed(request.state["messages"]) if isinstance(item, AIMessage))
        if len(latest.tool_calls) != 1:
            return tool_error(call, "invalid_arguments")
        if call["args"].get("approval_id") is not None:
            return tool_error(call, "approval_required")
        if any(isinstance(item, ToolMessage) and item.name in WRITE_TOOLS
               for item in current_turn(request.state["messages"])):
            return tool_error(call, "execution_limit_exceeded")

        key = hashlib.sha256(json.dumps([latest.id, call["id"]]).encode()).hexdigest()
        action = self.storage.action(self.user_id, self.session_id, call_key=key)
        if action is None:
            response = await handler(request)
            if response.status == "error":
                if RECOVERING_REVIEW.get():
                    # Keep the original tools checkpoint retryable; a review
                    # lookup must not continue into another model generation.
                    payload = json.loads(response.text)
                    raise PublicError(payload.get("code", "backend_unavailable"), outcome=payload.get("outcome"))
                return response
            proposal = json.loads(response.text)
            if proposal.get("status") != "pending" or proposal.get("operation") != call["name"]:
                raise PublicError("invalid_backend_response")
            action = self.storage.save_action(self.user_id, self.session_id, key, proposal)
        proposal = action["proposal"]

        # The tool node replays after resume. Its durable binding prevents restaging.
        choice = interrupt({"kind": "ticket_write", "proposal": proposal})
        action = self.storage.action(self.user_id, self.session_id, call_key=key)
        if (not isinstance(choice, dict) or choice.get("approval_id") != proposal["approval_id"]
                or choice.get("decision") not in {"approve", "decline"}
                or action["decision"] != choice["decision"]):
            raise PublicError("approval_required")
        if action["decision"] == "decline":
            result = {"status": "declined", "approval_id": proposal["approval_id"]}
            self.storage.finish(self.user_id, self.session_id, proposal["approval_id"], result)
            return ToolMessage(content=json.dumps(result), name=call["name"], tool_call_id=call["id"])

        approved = {**call, "args": {**proposal["arguments"], "approval_id": proposal["approval_id"]}}
        # A crash or cancellation after dispatch cannot leave an old not-applied receipt.
        self.storage.finish(self.user_id, self.session_id, proposal["approval_id"], {"outcome": "unknown"})
        response = await handler(request.override(tool_call=approved))
        self.storage.finish(self.user_id, self.session_id, proposal["approval_id"], json.loads(response.text))
        return response
