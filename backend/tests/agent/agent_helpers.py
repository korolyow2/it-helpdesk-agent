"""Share scripted models and a real MCP-backed agent test service."""

from contextlib import asynccontextmanager

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from pydantic import Field

from helpdesk.agent.service import open_service


class AllowGuardrail:
    """Bypass classification explicitly in unrelated deterministic protocol tests."""

    async def classify(self, message, history):
        """Allow the scripted turn without claiming model classification quality."""
        return "helpdesk"


class ScriptedModel(GenericFakeChatModel):
    """Emit predetermined decisions while recording the actual graph inputs."""

    seen: list = Field(default_factory=list)
    bindings: list = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        """Record the tools exposed by the application rather than simulate selection."""
        self.bindings.append([tool.name for tool in tools])
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        """Record history and return the next scripted model message."""
        self.seen.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        """Preserve scripted tool calls when the factory enables token streaming."""
        message = self._generate(messages, stop=stop, run_manager=run_manager, **kwargs).generations[0].message
        yield ChatGenerationChunk(message=AIMessageChunk(
            content=message.content, tool_calls=message.tool_calls,
            response_metadata=message.response_metadata,
            usage_metadata=message.usage_metadata,
        ))


def call(name, arguments, identifier="call-1"):
    """Construct one scripted tool decision for protocol verification."""
    return AIMessage(content="", tool_calls=[{"name": name, "args": arguments, "id": identifier}])


@asynccontextmanager
async def service(running, tmp_path, model, **kwargs):
    """Open an MCP-backed scripted service with an explicit test classifier."""
    kwargs.setdefault("guardrail", AllowGuardrail())
    async with open_service(
        model=model, mcp_url=running.url,
        user_tokens={f"user-{n:03}": f"mcp-token-{n}" for n in range(1, 5)},
        database=tmp_path / "agent.sqlite3", httpx_client_factory=running.http_client, **kwargs,
    ) as instance:
        yield instance


