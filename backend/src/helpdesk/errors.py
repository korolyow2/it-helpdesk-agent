"""Define minimal safe errors shared by HTTP and MCP."""

from contextvars import ContextVar
from dataclasses import dataclass
import json
from uuid import uuid4


REQUEST_ID: ContextVar[str | None] = ContextVar("helpdesk_request_id", default=None)


@dataclass(frozen=True)
class ErrorSpec:
    """Describe a public error without backend exception text or rejected inputs."""

    status: int
    message: str
    is_retryable: bool = False


ERRORS = {
    "authentication_required": ErrorSpec(401, "A verified MCP user is required."),
    "authentication_failed": ErrorSpec(401, "The user credential is missing, invalid, or no longer active."),
    "service_authentication_failed": ErrorSpec(401, "The backend service credential is invalid."),
    "access_denied": ErrorSpec(403, "This user does not have Helpdesk access."),
    "not_found_or_inaccessible": ErrorSpec(404, "The ticket does not exist or is not accessible."),
    "approval_not_found": ErrorSpec(404, "The approval does not exist in this user and session context."),
    "approval_required": ErrorSpec(409, "Explicit user approval is required before executing this action."),
    "approval_declined": ErrorSpec(409, "The user declined this action; it cannot be executed."),
    "approval_mismatch": ErrorSpec(409, "The action differs from the proposal that was approved."),
    "ticket_read_required": ErrorSpec(409, "Read the target ticket before proposing a comment."),
    "pending_approval_exists": ErrorSpec(409, "Another action is awaiting a decision in this session."),
    "approval_state_conflict": ErrorSpec(409, "This decision cannot be applied to the current approval state."),
    "data_conflict": ErrorSpec(409, "The change violates a data constraint and was rolled back."),
    "precondition_failed": ErrorSpec(412, "The resource state no longer meets the operation's conditions."),
    "invalid_arguments": ErrorSpec(422, "The request arguments are invalid; check the tool schema."),
    "session_required": ErrorSpec(422, "The application must provide a valid X-Session-Id header."),
    "request_too_large": ErrorSpec(413, "The request exceeds the supported size."),
    "method_not_allowed": ErrorSpec(405, "This backend operation does not support the HTTP method."),
    "request_rejected": ErrorSpec(400, "The backend rejected this request."),
    "unknown_tool": ErrorSpec(404, "The requested tool is not available."),
    "configuration_error": ErrorSpec(500, "The backend connection configuration is invalid."),
    "database_error": ErrorSpec(500, "The database operation failed; no successful outcome is confirmed."),
    "storage_unavailable": ErrorSpec(503, "Database storage requires operator attention."),
    "database_busy": ErrorSpec(503, "The database is temporarily busy.", True),
    "rate_limited": ErrorSpec(429, "The backend rate limit was reached.", True),
    "backend_unavailable": ErrorSpec(503, "The backend is temporarily unavailable.", True),
    "connection_failed": ErrorSpec(503, "The backend connection could not be established.", True),
    "backend_timeout": ErrorSpec(504, "The backend did not complete within a network timeout.", True),
    "deadline_exceeded": ErrorSpec(504, "The total operation time budget was exhausted.", True),
    "invalid_backend_response": ErrorSpec(502, "The backend returned an invalid or inconsistent response."),
    "backend_not_supported": ErrorSpec(502, "The backend does not support the required operation or protocol."),
    "context_limit_exceeded": ErrorSpec(413, "The current request and tool results exceed the context budget. Narrow the request or start a new session."),
    "model_refused": ErrorSpec(422, "The model declined to answer this request."),
    "guardrail_unavailable": ErrorSpec(503, "Input screening is temporarily unavailable. Please try again later.", True),
    "request_not_found": ErrorSpec(404, "Request details are unavailable for this user."),
    "incomplete_model_output": ErrorSpec(502, "The model output was truncated before completion."),
    "interrupted_read": ErrorSpec(503, "The previous read was interrupted and has no confirmed result.", True),
    "history_access_changed": ErrorSpec(403, "Earlier ticket data is no longer accessible. Start a new session."),
    "execution_limit_exceeded": ErrorSpec(429, "The agent reached its per-request execution limit."),
    "internal_error": ErrorSpec(500, "An unexpected internal error prevented completion."),
}


class PublicError(Exception):
    """Carry a minimal public failure and internal diagnostic metadata.

    Parameters
    ----------
    code : str
        Registered error code.
    request_id : str or None, optional
        Internal correlation ID, excluded from the agent payload.
    attempts : int, optional
        Internal backend attempt count, excluded from the agent payload.
    outcome : {'not_applied', 'unknown'} or None, optional
        Write outcome; omitted for read failures.
    retry_after : float or None, optional
        Validated wait for a rate-limited backend.
    """

    def __init__(self, code, *, request_id=None, attempts=1, outcome=None, retry_after=None):
        self.code = code
        self.spec = ERRORS[code]
        self.request_id = request_id or REQUEST_ID.get() or str(uuid4())
        self.attempts = attempts
        self.payload = {
            "is_error": True,
            "code": code,
            "message": self.spec.message,
            "is_retryable": self.spec.is_retryable,
        }
        if outcome is not None:
            self.payload["outcome"] = outcome
        if code == "rate_limited" and retry_after is not None:
            self.payload["retry_after_seconds"] = retry_after
        super().__init__(json.dumps(self.payload, separators=(",", ":")))
