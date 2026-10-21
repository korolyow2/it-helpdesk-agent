"""Run the read-only agent with user-bound MCP access and durable history."""

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
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import GraphRecursionError

from helpdesk.agent.graph import build_agent, model_from_env
from helpdesk.agent.guardrails.input import InputGuardrail
from helpdesk.agent.mcp_tools import MCPTools
from helpdesk.agent.observability import FirstTokenHandler
from helpdesk.agent.workflow import build_workflow
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
    """

    def __init__(self, model, checkpointer, mcp_url, user_tokens, *, deadline=60.0, httpx_client_factory=None,
                 guardrail=None):
        self.model = model
        self.guardrail = guardrail if guardrail is not None else InputGuardrail(model)
        self.checkpointer = checkpointer
        self.mcp_url = mcp_url
        self.user_tokens = dict(user_tokens)
        self.deadline = deadline
        self.httpx_client_factory = httpx_client_factory
        self.completed_tools = OrderedDict()
        # Serialize this small local service to avoid competing writes to one history.
        self.lock = asyncio.Lock()

    async def run(self, user_id, session_id, message):
        """Collect the streaming run for callers that need one final response.

        Parameters
        ----------
        user_id, session_id, message : str
            Trusted user, conversation, and user input.

        Returns
        -------
        dict
            Completed reply and tool results.
        """
        result = None
        async for event in self.stream(user_id, session_id, message):
            if event["event"] == "done":
                result = event["data"]
        if result is None:
            raise PublicError("internal_error")
        return result

    async def stream(self, user_id, session_id, message):
        """Trace one user turn while preserving answer-only public streaming.

        Parameters
        ----------
        user_id, session_id, message : str
            Application identity, conversation, and user input.

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
                          "approval": "read_only_stage"},
            ) as span:
                try:
                    async with aclosing(self._stream(user_id, session_id, message)) as events:
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
                                            metadata={"status": "blocked" if input_decision == "block" else "completed"})
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

    async def _stream(self, user_id, session_id, message):
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
        thread_id = hashlib.sha256(json.dumps([user_id, session_id]).encode()).hexdigest()
        request_id = REQUEST_ID.get() or str(uuid4())
        try:
            async with asyncio.timeout(self.deadline), self.lock:
                transport = StreamableHttpTransport(
                    self.mcp_url, auth=token,
                    headers={"X-Session-Id": session_id, "X-Request-Id": request_id, **trace_headers()},
                    httpx_client_factory=self.httpx_client_factory,
                )
                client = Client(transport, timeout=10, init_timeout=10, cache=False)
                capabilities = MCPTools(MCPAdapter(client))
                agent = build_agent(
                    self.model, [capabilities.discovery], self.checkpointer,
                    tool_middleware=capabilities,
                )
                config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}, "recursion_limit": 64,
                          "callbacks": [FirstTokenHandler()]}
                previous = await agent.aget_state(config)
                history = previous.values.get("messages", [])
                workflow = build_workflow(
                    self.guardrail, lambda message, history: self._agent_turn(agent, config, message, history),
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

    async def _agent_turn(self, agent, config, message, history):
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
        parts = agent.astream(
            {"messages": [
                *interrupted, {"role": "user", "content": message.strip()},
            ]},
            config, stream_mode=["messages", "values"], version="v2",
        )
        async with aclosing(parts):
            async for part in parts:
                if part["type"] == "values":
                    result = part["data"]
                elif part["type"] == "messages":
                    chunk, _metadata = part["data"]
                    if isinstance(chunk, AIMessageChunk) and chunk.text:
                        yield {"event": "text_delta", "data": {"message_id": chunk.id, "text": chunk.text}}
        if result is None:
            raise PublicError("internal_error", request_id=REQUEST_ID.get())
        messages = result["messages"]
        last = messages[-1]
        if not isinstance(last, AIMessage) or last.tool_calls or not last.text:
            raise PublicError("internal_error", request_id=REQUEST_ID.get())
        # Only expose this turn's calls; earlier messages remain in the checkpoint.
        start = next(index for index in range(len(messages) - 1, -1, -1) if messages[index].type == "human")
        arguments = {call["id"]: call["args"]
                     for item in messages[start:] if isinstance(item, AIMessage)
                     for call in item.tool_calls}
        calls = [
            {"name": item.name, "arguments": arguments.get(item.tool_call_id, {}),
             "status": item.status, "content": item.content}
            for item in messages[start:] if isinstance(item, ToolMessage)
        ]
        yield {"event": "done", "data": {"reply": last.text, "tool_calls": calls}}


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
    parser = argparse.ArgumentParser(description="Run one read-only Helpdesk agent turn.")
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
