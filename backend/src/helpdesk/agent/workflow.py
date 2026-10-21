"""Route transient input screening to the persistent ready-made ReAct agent."""

from contextlib import aclosing
from typing import NotRequired, TypedDict
from uuid import uuid4

from langchain_core.messages import BaseMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import StreamWriter

from helpdesk.agent.guardrails.input import BLOCKED_REPLY, INTENTS
from helpdesk.errors import PublicError


class RequestState(TypedDict):
    """Keep candidate input and routing decisions in memory for one request."""

    message: str
    history: list[BaseMessage]
    intent: NotRequired[str]


def build_workflow(guardrail, run_agent):
    """Connect input classification and agent execution with LangGraph routing.

    Parameters
    ----------
    guardrail : InputGuardrail
        Tool-free classifier; it cannot mutate conversation history.
    run_agent : callable
        Async event iterator accepting the approved message and stored history.
        It invokes the existing create_agent graph with its own checkpointer.

    Returns
    -------
    CompiledStateGraph
        Stateless request graph exposing answer events through custom streaming.
        Only the nested ReAct graph writes accepted conversation history.
    """
    async def classify(state: RequestState, writer: StreamWriter):
        """Return the intent and emit private trace metadata before routing."""
        intent = await guardrail.classify(state["message"], state["history"])
        if intent not in INTENTS.values():
            raise PublicError("guardrail_unavailable")
        writer({"event": "input_screened", "data": {
            "message": state["message"] if intent == "helpdesk" else None, "intent": intent,
            "code": next(int(code) for code, name in INTENTS.items() if name == intent),
            "decision": "allow" if intent == "helpdesk" else "block",
        }})
        return {"intent": intent}

    async def agent(state: RequestState, writer: StreamWriter):
        """Forward answer-only events from the persistent ReAct graph."""
        async with aclosing(run_agent(state["message"], state["history"])) as events:
            async for event in events:
                writer(event)
        return {}

    def blocked(state: RequestState, writer: StreamWriter):
        """Return a fixed reply without invoking the agent or changing history."""
        writer({"event": "text_delta", "data": {"message_id": str(uuid4()), "text": BLOCKED_REPLY}})
        writer({"event": "done", "data": {"reply": BLOCKED_REPLY, "tool_calls": []}})
        return {}

    builder = StateGraph(RequestState)
    builder.add_node("guardrail", classify)
    builder.add_node("agent", agent)
    builder.add_node("blocked", blocked)
    builder.add_edge(START, "guardrail")
    builder.add_conditional_edges("guardrail", lambda state: state["intent"], {
        "helpdesk": "agent", "blocked": "blocked",
    })
    builder.add_edge("agent", END)
    builder.add_edge("blocked", END)
    return builder.compile(checkpointer=False, name="helpdesk_request")
