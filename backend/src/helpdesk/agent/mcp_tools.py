"""Discover authorized MCP tools only after the model requests them."""

import json

from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import StructuredTool, ToolException

from helpdesk.errors import ERRORS, PublicError


READ_TOOLS = {"list_tickets", "search_tickets", "get_ticket"}


class MCPTools(AgentMiddleware):
    """Resolve tools dynamically within one user turn.

    Parameters
    ----------
    adapter : MCPAdapter
        Unconnected adapter carrying trusted user credentials. Its tools perform
        their own live authorization; discovery never grants lasting access.
    """

    def __init__(self, adapter):
        self.adapter = adapter
        self.tools = []
        self.discovery = StructuredTool.from_function(
            coroutine=self.discover,
            name="discover_tools",
            description=(
                "Load the Helpdesk read-tool schemas authorized for the current user. "
                "Call only when the request is clear enough to retrieve ticket information; "
                "ask a focused clarification first when the affected service or issue is unclear. "
                "This returns tool names, not tickets. After success, use list_tickets "
                "as the first ticket-data tool. Access denial ends tool use for this turn."
            ),
            args_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handle_tool_error=True,
        )

    async def discover(self):
        """Load authorized schemas without exposing write capabilities.

        Returns
        -------
        str
            Available read-tool names; no ticket content or identity credentials.

        Raises
        ------
        ToolException
            Minimal safe authorization or discovery failure for the model.
        """
        if not self.tools:
            try:
                tools = await self.adapter.list_tools(cache_mode="bypass")
                tools = [tool for tool in tools if tool.name in READ_TOOLS]
                if {tool.name for tool in tools} != READ_TOOLS:
                    raise PublicError("configuration_error")
            except Exception as error:
                code = error.code if isinstance(error, PublicError) else "backend_unavailable"
                data = getattr(error, "data", None)
                if isinstance(data, dict) and isinstance(data.get("error"), dict):
                    candidate = data["error"].get("code")
                    if isinstance(candidate, str) and candidate in ERRORS:
                        code = candidate
                raise ToolException(str(PublicError(code))) from None

            self.tools = tools

        # Distinguish the mandatory first step from capabilities unlocked next.
        return json.dumps({
            "available_tools": ["list_tickets"],
            "after_successful_overview": ["get_ticket", "search_tickets"],
        })

    async def awrap_model_call(self, request, handler):
        """Offer only capabilities discovered in the current user turn."""
        return await handler(request.override(tools=self.tools or [self.discovery]))

    async def awrap_tool_call(self, request, handler):
        """Resolve dynamic tools before argument validation and execution."""
        available = self.tools or [self.discovery]
        tool = next((tool for tool in available if tool.name == request.tool_call["name"]), None)
        return await handler(request.override(tool=tool))
