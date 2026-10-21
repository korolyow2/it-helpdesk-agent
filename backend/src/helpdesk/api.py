"""Expose authenticated chat requests and Server-Sent Events for a thin UI."""

from contextlib import aclosing, asynccontextmanager
import json
import os
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, StringConstraints

from helpdesk.agent.graph import model_from_env
from helpdesk.agent.service import open_service
from helpdesk.errors import PublicError
from helpdesk.paths import DATA_DIR, PROJECT_ROOT


class ChatRequest(BaseModel):
    """Validate user input while keeping authority in verified credentials."""

    model_config = ConfigDict(extra="forbid")
    user_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
    session_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")]
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10000)]


def create_app(*, service, user_tokens, lifespan=None):
    """Build chat endpoints that bind the supplied user ID to a verified token.

    Parameters
    ----------
    service : AgentService or None
        Open service, or one assigned during the configured lifespan.
    user_tokens : dict of str to str
        Configured MCP bearer tokens mapped to their authenticated user IDs.
    lifespan : callable or None, optional
        Application startup/shutdown manager.

    Returns
    -------
    FastAPI
        Application with POST /chat and POST /chat/stream.
    """
    app = FastAPI(title="IT Helpdesk Agent", lifespan=lifespan)
    app.state.service = service
    app.mount("/assets", StaticFiles(directory=PROJECT_ROOT / "frontend"), name="assets")
    tokens = dict(user_tokens)
    security = HTTPBearer(auto_error=False)

    def authenticate(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)]):
        """Resolve the actor from a bearer credential rather than the request body."""
        user_id = tokens.get(credentials.credentials) if credentials else None
        if user_id is None:
            raise PublicError("authentication_failed")
        return user_id

    User = Annotated[str, Depends(authenticate)]

    def check_user(body: ChatRequest, user_id: User):
        """Reject requests attempting to act as a different user."""
        if body.user_id != user_id:
            raise PublicError("authentication_failed")
        return user_id

    VerifiedUser = Annotated[str, Depends(check_user)]

    @app.get("/", include_in_schema=False)
    async def frontend():
        """Serve the local browser client without exposing credentials."""
        return FileResponse(PROJECT_ROOT / "frontend" / "index.html")

    @app.get("/chat/requests/{request_id}/tools")
    async def request_tools(request_id: str, user_id: User):
        """Fetch completed tool summaries separately from the answer stream."""
        return {"tools": app.state.service.request_tools(user_id, request_id)}

    @app.exception_handler(PublicError)
    async def public_error(request, error):
        """Return the minimal error before response headers have been sent."""
        return JSONResponse(error.payload, status_code=error.spec.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        """Exclude invalid values and unknown request keys from validation errors."""
        error = PublicError("invalid_arguments")
        return JSONResponse(error.payload, status_code=422)

    @app.post("/chat")
    async def chat(body: ChatRequest, user_id: VerifiedUser):
        """Collect a reply for clients that do not need streaming."""
        return await app.state.service.run(user_id, body.session_id, body.message)

    @app.post("/chat/stream", response_class=EventSourceResponse)
    async def chat_stream(body: ChatRequest, user_id: VerifiedUser):
        """Stream chat events; a failure is terminal and never followed by done."""
        try:
            async with aclosing(app.state.service.stream(user_id, body.session_id, body.message)) as stream:
                async for event in stream:
                    data = ({"reply": event["data"]["reply"], "request_id": event["data"]["request_id"]}
                            if event["event"] == "done" else {"text": event["data"]["text"]})
                    yield ServerSentEvent(event=event["event"], data=data)
        except PublicError as error:
            yield ServerSentEvent(event="error", data=error.payload)

    return app


def from_env():
    """Create the API with a managed model and SQLite service lifetime.

    Returns
    -------
    FastAPI
        Locally configured authenticated chat application.
    """
    tokens = json.loads(os.environ["MCP_USER_TOKENS"])

    @asynccontextmanager
    async def lifespan(app):
        """Open the model configuration and persistent agent state at startup."""
        async with open_service(
            model=model_from_env(), mcp_url=os.environ.get("MCP_URL", "http://127.0.0.1:8002/mcp"),
            user_tokens={user_id: token for token, user_id in tokens.items()},
            database=os.environ.get("AGENT_DATABASE", str(DATA_DIR / "runtime" / "agent.sqlite3")),
        ) as service:
            app.state.service = service
            yield

    return create_app(service=None, user_tokens=tokens, lifespan=lifespan)
