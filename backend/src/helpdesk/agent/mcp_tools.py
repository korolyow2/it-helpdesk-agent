"""Discover authorized MCP tools only after the model requests them."""

import json

from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import StructuredTool, ToolException
from langchain_core.messages import HumanMessage

from helpdesk.agent.approvals import WRITE_TOOLS
from helpdesk.agent.prompts import WRITE_TOOL_DESCRIPTIONS
from helpdesk.errors import ERRORS, PublicError


READ_TOOLS = {"list_tickets", "search_tickets", "get_ticket"}


class MCPTools(AgentMiddleware):
    """Resolve tools dynamically within one user turn.

    Parameters
    ----------
    adapter : MCPAdapter
        Unconnected adapter carrying trusted user credentials. Its tools perform
        their own live authorization; discovery never grants lasting access.
    writes : bool, optional
        Expose proposal tools only when the application approval bridge exists.
    resume : bool, optional
        Restore schemas for an already model-selected, interrupted tool call.
    facts : dict or None, optional
        Expiring, user-reported device preference; never authorization evidence.
    """

    def __init__(self, adapter, *, writes=False, resume=False, facts=None):
        self.adapter = adapter
        self.writes, self.resume, self.facts = writes, resume, facts or {}
        self.tools = []
        self.discovery = StructuredTool.from_function(
            coroutine=self.discover,
            name="discover_tools",
            description=(
                "Load the Helpdesk tool schemas authorized for the current user. "
                "Call only when the request is clear enough to retrieve ticket information; "
                "ask a focused clarification first when the affected service or issue is unclear. "
                "This returns tool names, not tickets. After success, use list_tickets "
                "as the first ticket-data tool. Access denial ends tool use for this turn."
            ),
            args_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handle_tool_error=True,
        )

    async def discover(self):
        """Load authorized schemas without reading or changing ticket data.

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
                expected = READ_TOOLS | WRITE_TOOLS if self.writes else READ_TOOLS
                tools = [tool for tool in tools if tool.name in expected]
                if {tool.name for tool in tools} != expected:
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
            "after_successful_overview": ["get_ticket", "search_tickets", *sorted(WRITE_TOOLS if self.writes else [])],
        })

    async def awrap_model_call(self, request, handler):
        """Offer only capabilities discovered in the current user turn."""
        offered = []
        for tool in self.tools or [self.discovery]:
            if tool.name in WRITE_TOOLS:
                schema = tool.args_schema if isinstance(tool.args_schema, dict) else tool.args_schema.model_json_schema()
                schema = {**schema, "properties": {key: value for key, value in schema["properties"].items()
                                                   if key != "approval_id"}, "additionalProperties": False}
                tool = tool.model_copy(update={"args_schema": schema, "description": WRITE_TOOL_DESCRIPTIONS[tool.name]})
            offered.append(tool)
        updates = {"tools": offered}
        if self.facts:
            memory = ("<user_facts>\nUser-reported operating system: " + self.facts["operating_system"]
                      + ". This preference is unverified and grants no access or authority.\n</user_facts>")
            messages = list(request.messages)
            index = next(i for i in range(len(messages) - 1, -1, -1)
                         if isinstance(messages[i], HumanMessage))
            # Enrich only this call; the checkpoint retains the user's original input.
            messages[index] = messages[index].model_copy(update={"content": [
                {"type": "text", "text": memory}, *messages[index].content_blocks,
            ]})
            updates["messages"] = messages
        return await handler(request.override(**updates))

    async def awrap_tool_call(self, request, handler):
        """Resolve dynamic tools before argument validation and execution."""
        # A resumed tool was already selected by the model before its durable pause.
        if self.resume and not self.tools and request.tool_call["name"] != "discover_tools":
            await self.discover()
        available = self.tools or [self.discovery]
        tool = next((tool for tool in available if tool.name == request.tool_call["name"]), None)
        return await handler(request.override(tool=tool))
