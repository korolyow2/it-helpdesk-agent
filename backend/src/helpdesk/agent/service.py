"""Run the Helpdesk agent with durable human review and user-scoped memory."""

import argparse

import anthropic
import asyncio
from collections import OrderedDict
from contextlib import aclosing, asynccontextmanager
import hashlib
import json
import logging
import os
from pathlib import Path
import re
from time import perf_counter
from uuid import uuid4

from fastmcp import Client
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain.agents.middleware.tool_call_limit import ToolCallLimitExceededError
from fastmcp.client.transports import StreamableHttpTransport
from langchain.mcp import MCPAdapter
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langchain_core.tools import ToolException
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import GraphRecursionError
from langgraph.types import Command

from helpdesk.agent.approvals import RECOVERING_REVIEW, ApprovalBridge, WriteReview, decision_from_message, proposal_reply
from helpdesk.agent.graph import build_agent, model_from_env
from helpdesk.agent.guardrails.input import InputGuardrail
from helpdesk.agent.mcp_tools import MCPTools
from helpdesk.agent.observability import FirstTokenHandler
from helpdesk.agent.workflow import build_workflow
from helpdesk.agent.storage import AppStorage
from helpdesk.agent.tool_policy import WRITE_TOOLS, invoke_mcp_safely
from helpdesk.mcp.mock_client import MockClient
from helpdesk.errors import ERRORS, REQUEST_ID, PublicError
from helpdesk.paths import DATA_DIR
from helpdesk.telemetry import observation, trace_headers, tracing_client


logger = logging.getLogger(__name__)


class AgentService:
    """Bind every run to trusted application identity and a separate MCP client.

    Parameters
    ----------
    model : BaseChatModel
        Model configured by the application, never by tool arguments.
    checkpointer : AsyncSqliteSaver
        Durable conversation storage separate from the mock ticket database.
    mcp_url : str
        Application-configured MCP HTTP endpoint.
    user_tokens : dict of str to str
        Trusted user IDs mapped to their individual MCP bearer tokens.
    deadline : float, optional
        Total seconds for discovery, model work, and tools.
    httpx_client_factory : callable or None, optional
        HTTP transport injection for integration tests.
    guardrail : InputGuardrail or None, optional
        Independent input classifier; defaults to the configured provider model.
    storage : AppStorage or None, optional
        Durable approval bindings and expiring facts, supplied by open_service.
    approval_bridge : ApprovalBridge or None, optional
        Application-only approval endpoints; absent configuration exposes reads only.
    """

    def __init__(self, model, checkpointer, mcp_url, user_tokens, *, deadline=60.0, httpx_client_factory=None,
                 guardrail=None, storage=None, approval_bridge=None):
        self.model = model
        self.guardrail = guardrail if guardrail is not None else InputGuardrail(model)
        self.checkpointer = checkpointer
        self.mcp_url = mcp_url
        self.user_tokens = dict(user_tokens)
        self.deadline = deadline
        self.httpx_client_factory = httpx_client_factory
        self.completed_tools = OrderedDict()
        self.storage, self.approvals = storage, approval_bridge
        # Serialize this small local service to avoid competing writes to one history.
        self.lock = asyncio.Lock()

    async def run(self, user_id, session_id, message, *, approval_id=None):
        """Collect the streaming run for callers that need one final response.

        Parameters
        ----------
        user_id, session_id, message : str
            Trusted user, conversation, and user input.
        approval_id : str or None, optional
            Exact displayed proposal for a browser decision; never a model argument.

        Returns
        -------
        dict
            Completed reply and tool results.
        """
        result = None
        async for event in self.stream(user_id, session_id, message, approval_id=approval_id):
            if event["event"] == "done":
                result = event["data"]
        if result is None:
            raise PublicError("internal_error")
        return result

    async def stream(self, user_id, session_id, message, *, approval_id=None):
        """Trace one user turn while preserving answer-only public streaming.

        Parameters
        ----------
        user_id, session_id, message : str
            Application identity, conversation, and user input.
        approval_id : str or None, optional
            Optional binding for an explicit approve/decline message.

        Yields
        ------
        dict
            Original service event; trace metadata stays out of chat text.
        """
        request_id = str(uuid4())
        correlation = REQUEST_ID.set(request_id)
        scoped_session = hashlib.sha256(json.dumps([user_id, session_id]).encode()).hexdigest()
        started = perf_counter()
        first_answer = True
        input_decision = None
        try:
            with observation(
                "helpdesk.request", as_type="agent", user_id=user_id,
                session_id=scoped_session,
                input={"input_status": "awaiting_screening"},
                metadata={"request_id": request_id, "model_call_limit": 6,
                          "tool_call_limit": 6, "deadline_seconds": self.deadline,
                          "approval": "human_review", "approval_id": approval_id},
            ) as span:
                try:
                    async with aclosing(self._stream(user_id, session_id, message, approval_id=approval_id)) as events:
                        async for event in events:
                            if event["event"] == "input_screened":
                                input_decision = event["data"]["decision"]
                                blocked = event["data"]["intent"] != "helpdesk"
                                trace_input = {"input_status": "blocked"} if blocked else {"message": event["data"]["message"]}
                                span.update(input=trace_input,
                                            metadata={"input_intent": event["data"]["intent"],
                                                      "guardrail_code": event["data"]["code"],
                                                      "guardrail_decision": input_decision})
                                continue
                            if event["event"] == "text_delta" and first_answer:
                                span.update(metadata={"first_answer_token_ms": (perf_counter() - started) * 1000})
                                first_answer = False
                            if event["event"] == "done":
                                self.completed_tools[request_id] = (user_id, [
                                    {"name": call["name"], "status": call["status"]}
                                    for call in event["data"]["tool_calls"]
                                ])
                                if len(self.completed_tools) > 128:
                                    self.completed_tools.popitem(last=False)
                                span.update(output={"reply": event["data"]["reply"]},
                                            metadata={"status": "blocked" if input_decision == "block" else
                                                      "awaiting_approval" if event["data"].get("approval") else "completed"})
                                parent = trace_headers().get("traceparent")
                                event = {**event, "data": {
                                    **event["data"], "request_id": request_id,
                                    "trace_id": parent.split("-")[1] if parent else None,
                                }}
                            yield event
                except PublicError as error:
                    metadata = {"status": "failed"}
                    if error.code == "guardrail_unavailable":
                        metadata["guardrail_decision"] = "error"
                    span.update(metadata=metadata)
                    raise
        finally:
            REQUEST_ID.reset(correlation)

    async def _stream(self, user_id, session_id, message, *, approval_id=None):
        """Yield text and completion with internal metadata for application consumers.

        Parameters
        ----------
        user_id, session_id : str
            Trusted application user and conversation identifiers.
        message : str
            User input, limited to 10,000 characters.

        Yields
        ------
        dict
            Text event or final result. HTTP/CLI omit internal completion metadata.

        Raises
        ------
        PublicError
            Safe failure; completion is never emitted after an unresolved error.
        """
        if (not isinstance(session_id, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", session_id) is None
                or not isinstance(message, str) or not message.strip() or len(message) > 10000):
            raise PublicError("invalid_arguments", attempts=0)
        token = self.user_tokens.get(user_id) if isinstance(user_id, str) else None
        if token is None:
            raise PublicError("authentication_failed", attempts=0)
        request_id = REQUEST_ID.get() or str(uuid4())
        try:
            async with asyncio.timeout(self.deadline), self.lock:
                capabilities, agent, config = self.conversation(user_id, session_id, request_id)
                previous = await agent.aget_state(config)
                history = previous.values.get("messages", [])
                capabilities.resume = bool(previous.interrupts)

                async def accepted(candidate, stored_history):
                    """Process only screened input and application-verified decisions."""
                    nonlocal previous
                    await asyncio.to_thread(self.storage.learn, user_id, candidate)
                    capabilities.facts = await asyncio.to_thread(self.storage.facts, user_id)
                    if self.unfinished_proposal(previous):
                        previous = await self.recover_review(agent, config, previous, capabilities, user_id, session_id)
                        # Recovery must show the exact proposal before accepting a decision.
                        if previous.interrupts:
                            for event in self.proposal_events(previous.interrupts[0].value["proposal"]):
                                yield event
                            return
                    decision = decision_from_message(candidate)
                    resume = None
                    if previous.interrupts:
                        proposal = previous.interrupts[0].value["proposal"]
                        if approval_id is not None and approval_id != proposal["approval_id"]:
                            raise PublicError("approval_mismatch")
                        if decision is None:
                            for event in self.proposal_events(proposal):
                                yield event
                            return
                        await self.approvals.decide(user_id, session_id, proposal, decision)
                        await asyncio.to_thread(
                            self.storage.decide, user_id, session_id, proposal["approval_id"], decision,
                        )
                        resume = {"approval_id": proposal["approval_id"], "decision": decision}
                    elif decision is not None:
                        if approval_id:
                            action = await asyncio.to_thread(
                                self.storage.action, user_id, session_id, approval_id=approval_id,
                            )
                        else:
                            action = await asyncio.to_thread(self.storage.latest_action, user_id, session_id)
                        if action is None or self.approvals is None:
                            raise PublicError("approval_not_found")
                        async for event in self.repeat_decision(user_id, session_id, action, decision, capabilities):
                            yield event
                        return
                    elif approval_id is not None:
                        raise PublicError("approval_required")
                    async for event in self._agent_turn(agent, config, candidate, stored_history, resume=resume):
                        yield event

                workflow = build_workflow(
                    self.guardrail, accepted,
                )
                parts = workflow.astream(
                    {"message": message.strip(), "history": history},
                    stream_mode="custom", version="v2",
                )
                async with aclosing(parts):
                    async for part in parts:
                        yield part["data"]
        except PublicError:
            raise
        except (ModelCallLimitExceededError, ToolCallLimitExceededError, GraphRecursionError):
            raise PublicError("execution_limit_exceeded", request_id=request_id) from None
        except anthropic.APITimeoutError:
            raise PublicError("backend_timeout", request_id=request_id) from None
        except anthropic.APIConnectionError:
            raise PublicError("backend_unavailable", request_id=request_id) from None
        except anthropic.APIStatusError as error:
            code = ("configuration_error" if error.status_code in {401, 403} else
                    "rate_limited" if error.status_code == 429 else
                    "backend_unavailable" if error.status_code >= 500 else "invalid_arguments")
            logger.warning("model_failure code=%s request_id=%s", code, request_id)
            raise PublicError(code, request_id=request_id) from None
        except TimeoutError:
            logger.warning("agent_timeout request_id=%s", request_id)
            raise PublicError("deadline_exceeded", request_id=request_id) from None
        except Exception as error:
            # MCP discovery errors may carry our safe JSON inside a JSON-RPC error.
            data = getattr(error, "data", None)
            if isinstance(data, dict) and isinstance(data.get("error"), dict):
                code = data["error"].get("code")
                if isinstance(code, str) and code in ERRORS:
                    raise PublicError(code, request_id=request_id) from None
            logger.warning("agent_failure request_id=%s exception_type=%s", request_id, type(error).__name__)
            raise PublicError("internal_error", request_id=request_id) from None

    def request_tools(self, user_id, request_id):
        """Return a bounded completed request's tool names and statuses.

        Parameters
        ----------
        user_id : str
            Authenticated actor, never derived from the request ID.
        request_id : str
            Completed request identifier returned after answer streaming.

        Returns
        -------
        list of dict
            Tool names and outcomes without arguments or ticket payloads.

        Raises
        ------
        PublicError
            If the request expired or belongs to another user.
        """
        record = self.completed_tools.get(request_id)
        if record is None or record[0] != user_id:
            raise PublicError("request_not_found")
        return record[1]

    @staticmethod
    def proposal_events(proposal, calls=None):
        """Render a pending review outside the public token stream's tool payloads."""
        reply = proposal_reply(proposal)
        yield {"event": "text_delta", "data": {"message_id": str(uuid4()), "text": reply}}
        yield {"event": "done", "data": {"reply": reply, "tool_calls": calls or [], "approval": proposal}}

    def conversation(self, user_id, session_id, request_id):
        """Build an unconnected, credential-bound graph for a conversation checkpoint."""
        thread_id = hashlib.sha256(json.dumps([user_id, session_id]).encode()).hexdigest()
        transport = StreamableHttpTransport(
            self.mcp_url, auth=self.user_tokens[user_id],
            headers={"X-Session-Id": session_id, "X-Request-Id": request_id, **trace_headers()},
            httpx_client_factory=self.httpx_client_factory,
        )
        # Leave time for the backend's ten-second error to cross MCP.
        client = Client(transport, timeout=12, init_timeout=12, cache=False)
        capabilities = MCPTools(MCPAdapter(client), writes=self.approvals is not None)
        agent = build_agent(
            self.model, [capabilities.discovery], self.checkpointer,
            tool_middleware=capabilities,
            write_middleware=WriteReview(self.storage, user_id, session_id) if self.approvals else None,
        )
        config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}, "recursion_limit": 64,
                  "callbacks": [FirstTokenHandler()]}
        return capabilities, agent, config

    @staticmethod
    def unfinished_proposal(snapshot):
        """Recognize a checkpointed write that failed before its native review pause."""
        messages = snapshot.values.get("messages", [])
        if snapshot.interrupts or "tools" not in snapshot.next or not messages:
            return False
        last = messages[-1]
        return (isinstance(last, AIMessage) and len(last.tool_calls) == 1
                and last.tool_calls[0]["name"] in WRITE_TOOLS
                and not last.tool_calls[0]["args"].get("approval_id"))

    async def recover_review(self, agent, config, snapshot, capabilities, user_id, session_id):
        """Replay only the saved proposal node, deduplicating its exact backend action."""
        if not self.unfinished_proposal(snapshot):
            return snapshot
        latest = snapshot.values["messages"][-1]
        key = hashlib.sha256(json.dumps([latest.id, latest.tool_calls[0]["id"]]).encode()).hexdigest()
        action = await asyncio.to_thread(self.storage.action, user_id, session_id, call_key=key)
        if action is not None and action["decision"] is not None:
            return snapshot
        capabilities.resume = True
        with observation("approval.recover", metadata={"source": "checkpointed_model_call"}) as span:
            # The saved model call is durable before tool execution. Never run a
            # new model or an approved mutation while restoring its review.
            token = RECOVERING_REVIEW.set(True)
            try:
                await agent.ainvoke(None, config, interrupt_after=["tools"])
            except ToolException as error:
                try:
                    payload = json.loads(str(error))
                except (ValueError, TypeError):
                    payload = {}
                code = payload.get("code") if isinstance(payload, dict) else None
                raise PublicError(code if isinstance(code, str) and code in ERRORS else "backend_unavailable") from None
            finally:
                RECOVERING_REVIEW.reset(token)
            restored = await agent.aget_state(config)
            if not restored.interrupts:
                last = restored.values.get("messages", [])[-1]
                if isinstance(last, ToolMessage) and last.status == "error":
                    payload = json.loads(last.text)
                    raise PublicError(payload.get("code", "backend_unavailable"), outcome=payload.get("outcome"))
                raise PublicError("invalid_backend_response")
            span.update(output={"status": "pending", "approval_id": restored.interrupts[0].value["proposal"]["approval_id"]})
            return restored

    async def pending_approval(self, user_id, session_id):
        """Restore the exact review, including a crash before saving its local binding."""
        if user_id not in self.user_tokens:
            raise PublicError("authentication_failed")
        try:
            async with asyncio.timeout(self.deadline), self.lock:
                if self.approvals is not None:
                    capabilities, agent, config = self.conversation(user_id, session_id, REQUEST_ID.get() or str(uuid4()))
                    snapshot = await agent.aget_state(config)
                    await self.recover_review(agent, config, snapshot, capabilities, user_id, session_id)
                return await self.review_state(user_id, session_id)
        except TimeoutError:
            raise PublicError("deadline_exceeded") from None

    async def review_state(self, user_id, session_id):
        """Read current backend authority and reconcile a previously uncertain receipt."""
        action = await asyncio.to_thread(self.storage.latest_action, user_id, session_id)
        if action is None or self.approvals is None:
            return None
        proposal = await self.approvals.get(user_id, session_id, action["approval_id"])
        if proposal["status"] == "completed" and (action.get("result") or {}).get("outcome") == "unknown":
            with observation("approval.outcome_resolved", output={"approval_id": action["approval_id"], "status": "completed"}):
                await asyncio.to_thread(
                    self.storage.finish, user_id, session_id, action["approval_id"], {"status": "completed"},
                )
            return proposal
        return proposal if proposal["status"] in {"pending", "approved"} else None

    async def repeat_decision(self, user_id, session_id, action, decision, capabilities):
        """Replay only the original model-selected action after an explicit retry."""
        if action["decision"] not in {None, decision}:
            raise PublicError("approval_state_conflict")
        proposal = action["proposal"]
        current = await self.approvals.decide(user_id, session_id, proposal, decision)
        await asyncio.to_thread(self.storage.decide, user_id, session_id, action["approval_id"], decision)
        calls = []
        if current["status"] == "completed":
            reply = "The approved action is already completed. No additional change was made."
        elif decision == "decline":
            reply = "The proposal was declined. No change was made."
        else:
            # Reconciliation is a replay of an already selected, approved action.
            # It never asks the model for a replacement action or idempotency key.
            await capabilities.discover()
            tool = next(item for item in capabilities.tools if item.name == proposal["operation"])
            arguments = {**proposal["arguments"], "approval_id": proposal["approval_id"]}
            with observation("approval.reconcile", as_type="tool", input={"approval_id": proposal["approval_id"]}) as span:
                call = {"type": "tool_call", "name": tool.name, "id": f"reconcile-{uuid4()}", "args": arguments}
                await asyncio.to_thread(self.storage.finish, user_id, session_id, proposal["approval_id"], {"outcome": "unknown"})
                result = await invoke_mcp_safely(call, lambda: tool.ainvoke(call))
                data = json.loads(result.text)
                await asyncio.to_thread(self.storage.finish, user_id, session_id, proposal["approval_id"], data)
                span.update(output=data)
                if result.status == "error":
                    raise PublicError(data.get("code", "backend_unavailable"), outcome=data.get("outcome"))
            calls = [{"name": tool.name, "arguments": arguments, "status": data.get("status", result.status), "content": result.content}]
            reply = "The approved action is confirmed completed. No duplicate change was made."
        yield {"event": "text_delta", "data": {"message_id": str(uuid4()), "text": reply}}
        yield {"event": "done", "data": {"reply": reply, "tool_calls": calls}}

    async def _agent_turn(self, agent, config, message, history, *, resume=None):
        """Stream an accepted turn through the persistent create_agent graph.

        Parameters
        ----------
        agent : CompiledStateGraph
            Ready-made ReAct graph with the application's SQLite checkpointer.
        config : dict
            Verified user/session thread with an explicit root checkpoint namespace.
        message : str
            Current message already accepted by the input guardrail.
        history : list of BaseMessage
            Existing user-scoped history used to repair interrupted read pairs.
        resume : dict or None, optional
            Application-verified approval decision for a paused tool node.

        Yields
        ------
        dict
            Answer text and a completion result; thinking is never forwarded.
        """
        answered = {item.tool_call_id for item in history if isinstance(item, ToolMessage)}
        interrupted = [
            ToolMessage(content=str(PublicError("interrupted_read")), status="error",
                        tool_call_id=call["id"], name=call["name"])
            for item in history if isinstance(item, AIMessage)
            for call in item.tool_calls if call["id"] not in answered
        ]
        # Complete interrupted read calls before adding the new user message.
        result = None
        text_chunks = {}
        graph_input = Command(resume=resume) if resume else {"messages": [
                *interrupted, {"role": "user", "content": message.strip()},
            ]}
        parts = agent.astream(
            graph_input,
            config, stream_mode=["messages", "values"], version="v2",
        )
        async with aclosing(parts):
            async for part in parts:
                if part["type"] == "values":
                    result = part["data"]
                elif part["type"] == "messages":
                    chunk, _metadata = part["data"]
                    if isinstance(chunk, AIMessageChunk) and chunk.text:
                        text_chunks.setdefault(chunk.id, []).append(chunk.text)
        snapshot = await agent.aget_state(config)
        if snapshot.interrupts:
            proposal = snapshot.interrupts[0].value["proposal"]
            calls = self.turn_tool_calls(snapshot.values.get("messages", []))
            calls.append({"name": proposal["operation"], "arguments": proposal["arguments"],
                          "status": "pending", "content": json.dumps(proposal)})
            for event in self.proposal_events(proposal, calls):
                yield event
            return
        if result is None:
            raise PublicError("internal_error", request_id=REQUEST_ID.get())
        messages = result["messages"]
        last = messages[-1]
        if not isinstance(last, AIMessage) or last.tool_calls or not last.text:
            raise PublicError("internal_error", request_id=REQUEST_ID.get())
        # A model can emit text before deciding to call a tool. Publish only
        # the validated final message, never text from those intermediate steps.
        chunks = text_chunks.get(last.id, [])
        if "".join(chunks) != last.text:
            chunks = [last.text]
        for text in chunks:
            yield {"event": "text_delta", "data": {"message_id": last.id, "text": text}}
        calls = self.turn_tool_calls(messages)
        if resume:
            calls = [call for call in calls if call["name"] in {"create_ticket", "add_comment"}]
        yield {"event": "done", "data": {"reply": last.text, "tool_calls": calls}}

    @staticmethod
    def turn_tool_calls(messages):
        """Expose actual current-turn outcomes without fabricating model decisions."""
        start = next(index for index in range(len(messages) - 1, -1, -1) if messages[index].type == "human")
        arguments = {call["id"]: call["args"]
                     for item in messages[start:] if isinstance(item, AIMessage)
                     for call in item.tool_calls}
        calls = []
        for item in messages[start:]:
            if not isinstance(item, ToolMessage):
                continue
            status = item.status
            if item.name in {"create_ticket", "add_comment"} and status == "success":
                status = json.loads(item.text).get("status", status)
            calls.append({"name": item.name, "arguments": arguments.get(item.tool_call_id, {}),
                          "status": status, "content": item.content})
        return calls


@asynccontextmanager
async def open_service(*, model, mcp_url, user_tokens, database, **kwargs):
    """Keep the SQLite checkpointer open for the application's service lifetime.

    Parameters
    ----------
    model : BaseChatModel
        Configured model.
    mcp_url : str
        Trusted MCP HTTP endpoint.
    user_tokens : dict of str to str
        Trusted user IDs mapped to their MCP bearer tokens.
    database : pathlib.Path
        Agent-state SQLite file, separate from the mock database.
    **kwargs : dict
        Optional AgentService execution settings.

    Yields
    ------
    AgentService
        Service whose checkpointer is closed on exit.
    """
    database = Path(database)
    database.parent.mkdir(parents=True, exist_ok=True)
    kwargs.setdefault("storage", AppStorage(database))
    if "approval_bridge" not in kwargs and os.environ.get("MOCK_SERVICE_KEY") and os.environ.get("MOCK_USER_TOKENS"):
        credentials = json.loads(os.environ["MOCK_USER_TOKENS"])
        kwargs["approval_bridge"] = ApprovalBridge(MockClient(
            os.environ.get("MOCK_API_URL", "http://127.0.0.1:8001"), os.environ["MOCK_SERVICE_KEY"],
            {user: token for token, user in credentials.items()},
        ))
    client = tracing_client()
    async with AsyncSqliteSaver.from_conn_string(str(database)) as checkpointer:
        try:
            yield AgentService(model, checkpointer, mcp_url, user_tokens, **kwargs)
        finally:
            if client is not None:
                try:
                    await asyncio.wait_for(asyncio.to_thread(client.flush), timeout=5)
                except Exception:
                    logger.warning("telemetry_flush_failed")


async def main():
    """Run one trusted local CLI message; this is not an authenticated web API."""
    parser = argparse.ArgumentParser(description="Run one Helpdesk agent turn, or explicitly approve/decline its proposal.")
    parser.add_argument("--user", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--message", required=True)
    parser.add_argument("--stream", action="store_true", help="Print streaming events as JSON lines.")
    args = parser.parse_args()
    try:
        tokens = json.loads(os.environ["MCP_USER_TOKENS"])
        async with open_service(
            model=model_from_env(), mcp_url=os.environ.get("MCP_URL", "http://127.0.0.1:8002/mcp"),
            user_tokens={user: token for token, user in tokens.items()},
            database=os.environ.get("AGENT_DATABASE", str(DATA_DIR / "runtime" / "agent.sqlite3")),
        ) as service:
            if args.stream:
                async for event in service.stream(args.user, args.session, args.message):
                    data = {"reply": event["data"]["reply"]} if event["event"] == "done" else {"text": event["data"]["text"]}
                    print(json.dumps({"event": event["event"], "data": data}), flush=True)
            else:
                print(json.dumps(await service.run(args.user, args.session, args.message)))
    except PublicError as error:
        print(str(error))
        raise SystemExit(1) from None
    except (KeyError, ValueError):
        print(str(PublicError("configuration_error")))
        raise SystemExit(1) from None


if __name__ == "__main__":
    asyncio.run(main())
