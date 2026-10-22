"""Expose authenticated ticket operations and application-only approval decisions."""

import asyncio
import json
import os
from pathlib import Path
from secrets import compare_digest
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Query, Request, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from pydantic import TypeAdapter, ValidationError

from helpdesk.errors import PublicError
from helpdesk.mock.errors import install_error_handlers
from helpdesk.mock.models import AddComment, ApprovalDecision, CreateTicket, Ticket, TicketId
from helpdesk.mock.failures import FailureConfig, FailureSwitch
from helpdesk.paths import DEFAULT_DATABASE
from helpdesk.mock.store import TicketStore
from helpdesk.telemetry import observation, remote_environment, remote_parent


SessionHeader = Annotated[
    str, Header(alias="X-Session-Id", pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
]


def create_app(*, database: Path, service_key: str, user_tokens: dict[str, str], failure_config=None) -> FastAPI:
    """Create the mock API with service and user authentication.

    Parameters
    ----------
    database : pathlib.Path
        Initialized SQLite database, including the approval schema.
    service_key : str
        Shared credential required for MCP-to-mock requests.
    user_tokens : dict of str to str
        Mapping from mock-specific bearer tokens to user IDs.
    failure_config : FailureConfig or None, optional
        Initial finite fault settings; defaults to disabled.

    Returns
    -------
    FastAPI
        Permission-checked mock application.

    Raises
    ------
    ValueError
        If credential configuration is empty or invalid.
    """
    if not service_key or not isinstance(user_tokens, dict) or not user_tokens or not all(
        isinstance(token, str) and token and isinstance(user_id, str) and user_id
        for token, user_id in user_tokens.items()
    ):
        raise ValueError("Configure a service key and non-empty user token mappings.")

    app = FastAPI(title="Mock Helpdesk API")
    install_error_handlers(app)
    store = TicketStore(database)
    tokens = dict(user_tokens)
    app.state.failures = FailureSwitch(failure_config)

    @app.middleware("http")
    async def trace_request(request, call_next):
        """Correlate mock operations without recording credential headers."""
        with observation(
            "mock.http", trace_context=remote_parent(request.headers),
            environment=remote_environment(request.headers),
            metadata={"method": request.method, "path": request.url.path,
                      "request_id": request.headers.get("x-request-id")},
        ) as span:
            response = await call_next(request)
            fault = getattr(request.state, "mock_fault", None)
            if fault is not None and fault.mode == "committed_timeout" and 200 <= response.status_code < 300:
                async for _ in response.body_iterator:
                    pass
                response = JSONResponse({"error": PublicError("backend_timeout", outcome="unknown").payload}, status_code=504)
            span.update(output={"http_status": response.status_code})
            if response.status_code >= 400:
                span.update(level="ERROR", status_message=f"http_{response.status_code}")
            return response

    service_auth = APIKeyHeader(
        name="X-Mock-Service-Key", scheme_name="MockServiceKey", auto_error=False
    )
    user_auth = HTTPBearer(scheme_name="MockUserToken", auto_error=False)

    def authenticated_user(
        key: Annotated[str | None, Security(service_auth)],
        credentials: Annotated[HTTPAuthorizationCredentials | None, Security(user_auth)],
    ) -> str:
        """Resolve identity from both credentials, never from model arguments.

        Parameters
        ----------
        key : str or None
            Service credential extracted from the request.
        credentials : HTTPAuthorizationCredentials or None
            Mock-specific user credential.

        Returns
        -------
        str
            Verified user ID.

        Raises
        ------
        PublicError
            If either credential is missing or invalid.
        """
        if key is None or not compare_digest(key.encode(), service_key.encode()):
            raise PublicError("service_authentication_failed")
        user_id = tokens.get(credentials.credentials) if credentials else None
        if user_id is None:
            raise PublicError("authentication_failed")
        return user_id

    User = Annotated[str, Depends(authenticated_user)]

    async def ticket_user(request: Request, user_id: User):
        """Authorize before injecting a fault; ticket checks still run in the store."""
        with store.connect() as connection:
            role = store.authorize(connection, user_id)
            if "ticket_id" in request.path_params:
                try:
                    ticket_id = TypeAdapter(TicketId).validate_python(request.path_params["ticket_id"])
                except ValidationError:
                    return user_id
                store.visible_ticket(connection, role, ticket_id)
        payload = await request.json() if request.method == "POST" else {}
        fault = app.state.failures.take(request.method, payload)
        request.state.mock_fault = fault
        if fault is not None:
            with observation("mock.synthetic_fault", metadata={"mode": fault.mode, "remaining": app.state.failures.config.remaining}):
                if fault.mode == "rate_limit":
                    raise PublicError("rate_limited", retry_after=fault.retry_after_seconds)
                if fault.mode == "server_error":
                    raise PublicError("backend_unavailable")
                if fault.mode == "slow":
                    await asyncio.sleep(fault.delay_seconds)
        return user_id

    TicketUser = Annotated[str, Depends(ticket_user)]

    def operator(key: Annotated[str | None, Security(service_auth)]):
        """Require the operator-held service key, never a browser user token."""
        if key is None or not compare_digest(key.encode(), service_key.encode()):
            raise PublicError("service_authentication_failed")

    @app.get("/health", include_in_schema=False)
    def health():
        """Check storage readiness without exposing users or tickets."""
        with store.connect() as connection:
            connection.execute("SELECT 1 FROM users LIMIT 1").fetchone()
        return {"status": "ok"}

    @app.get("/admin/failures", dependencies=[Depends(operator)])
    def failure_status():
        """Inspect the finite runtime fault configuration."""
        return app.state.failures.config.model_dump()

    @app.put("/admin/failures", dependencies=[Depends(operator)])
    def set_failures(config: FailureConfig):
        """Replace runtime faults; this operation is never an MCP tool."""
        app.state.failures.config = config
        return config.model_dump()

    @app.get("/access")
    def access(user_id: User) -> dict:
        """Check current Helpdesk membership without returning ticket data."""
        with store.connect() as connection:
            role = store.authorize(connection, user_id)
        return {"user_id": user_id, "role": role}

    @app.get("/tickets")
    def search_tickets(
        user_id: TicketUser,
        query: Annotated[str, Query(min_length=1, max_length=500)],
        page: Annotated[int, Query(ge=1, le=1000000)] = 1,
        page_size: Annotated[int, Query(ge=1, le=50)] = 5,
    ) -> dict:
        """Search visible tickets before applying ranking and pagination."""
        return store.search(user_id, query, page, page_size)

    @app.get("/tickets/overview")
    def list_tickets(
        user_id: TicketUser,
        page: Annotated[int, Query(ge=1, le=1000000)] = 1,
        page_size: Annotated[int, Query(ge=1, le=50)] = 50,
    ) -> dict:
        """List permission-filtered names, IDs, and statuses without ticket bodies."""
        return store.list_tickets(user_id, page, page_size)

    @app.get("/tickets/{ticket_id}", response_model=Ticket)
    def get_ticket(ticket_id: TicketId, user_id: TicketUser) -> dict:
        """Read a permitted ticket or return a non-disclosing 404 response."""
        return store.get_ticket(user_id, ticket_id)

    @app.post("/tickets")
    def create_ticket(body: CreateTicket, user_id: TicketUser, session_id: SessionHeader) -> dict:
        """Propose ticket creation, or execute its exact approved action."""
        return store.write(user_id, session_id, "create_ticket",
                           body.model_dump(exclude={"approval_id"}), body.approval_id)

    @app.post("/tickets/{ticket_id}/comments")
    def add_comment(ticket_id: TicketId, body: AddComment, user_id: TicketUser, session_id: SessionHeader) -> dict:
        """Propose a comment, or execute its exact approved action."""
        payload = {"ticket_id": ticket_id, "body": body.body}
        return store.write(user_id, session_id, "add_comment", payload, body.approval_id)

    @app.get("/approvals/{approval_id}")
    def get_approval(approval_id: str, user_id: User, session_id: SessionHeader) -> dict:
        """Restore an approval for the same authenticated user and session."""
        return store.get_approval(user_id, session_id, approval_id)

    @app.post("/approvals/{approval_id}/decision")
    def decide_approval(
        approval_id: str, body: ApprovalDecision, user_id: User, session_id: SessionHeader,
    ) -> dict:
        """Record an explicit user decision; this endpoint is never an MCP tool."""
        with observation(
            "approval.decision", input={"approval_id": approval_id, "decision": body.decision},
            metadata={"acting_user": user_id},
        ) as span:
            result = store.decide(user_id, session_id, approval_id, body.decision)
            span.update(output=result)
            return result

    # Both credentials are required, not alternatives as FastAPI generates by default.
    for path in app.openapi()["paths"].values():
        for operation in path.values():
            schemes = {key for item in operation.get("security", []) for key in item}
            if schemes == {"MockServiceKey", "MockUserToken"}:
                operation["security"] = [{"MockServiceKey": [], "MockUserToken": []}]
    return app


def from_env() -> FastAPI:
    """Build the mock API from environment configuration.

    Returns
    -------
    FastAPI
        Configured mock application.

    Raises
    ------
    KeyError
        If a required credential variable is missing.
    ValueError
        If credential configuration is invalid, including malformed JSON.
    """
    return create_app(
        database=Path(os.environ.get("MOCK_DATABASE", str(DEFAULT_DATABASE))),
        service_key=os.environ["MOCK_SERVICE_KEY"],
        user_tokens=json.loads(os.environ["MOCK_USER_TOKENS"]),
        failure_config=FailureConfig(
            mode=os.environ.get("MOCK_FAILURE_MODE", "off"),
            remaining=int(os.environ.get("MOCK_FAILURE_COUNT", "2")),
            delay_seconds=float(os.environ.get("MOCK_FAILURE_DELAY", "4")),
        ),
    )
