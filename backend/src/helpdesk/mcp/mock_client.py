"""Call the mock with bounded retries, validated responses, and minimal errors."""

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import logging
import math
import random
import ssl
from uuid import uuid4

import httpx2 as httpx
from pydantic import ValidationError

from helpdesk.errors import ERRORS, REQUEST_ID, PublicError
from helpdesk.telemetry import observation, trace_headers
from helpdesk.mcp.errors import ToolFailure
from helpdesk.mock.models import (
    AccessResult, AddComment, AddedCommentResult, ApprovalState, CreateTicket, CreatedTicketResult,
    SearchResult, Ticket, TicketListResult, WriteProposal,
)


logger = logging.getLogger(__name__)
RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}
MAX_RESPONSE_BYTES = 1024 * 1024
OPERATION_DEADLINE: ContextVar[float | None] = ContextVar("helpdesk_operation_deadline", default=None)


@asynccontextmanager
async def traced_attempt(client, method, path, attempt, **kwargs):
    """Measure one HTTP attempt through receipt of the streamed response body.

    Parameters
    ----------
    client : AsyncClient
        Credential-bound HTTP client; its headers are never recorded.
    method, path : str
        Application-defined operation.
    attempt : int
        One-based attempt number.
    **kwargs : dict
        Request arguments passed through unchanged.

    Yields
    ------
    Response
        Streamed HTTP response.
    """
    with observation(
        "backend.http_attempt", metadata={"method": method, "path": path, "attempt": attempt},
    ) as span:
        kwargs["headers"] = {**kwargs.get("headers", {}), **trace_headers()}
        async with client.stream(method, path, **kwargs) as response:
            span.update(metadata={"http_status": response.status_code})
            if response.status_code >= 400:
                span.update(level="WARNING", status_message=f"http_{response.status_code}")
            yield response


def retry_after_seconds(value):
    """Parse nonnegative finite seconds or an HTTP date without trusting invalid values.

    Parameters
    ----------
    value : str or None
        Retry-After header value.

    Returns
    -------
    float or None
        Required wait, or no usable instruction.
    """
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            seconds = (date - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
        return max(0.0, seconds)
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def transport_error_code(error):
    """Classify transport failures without matching or exposing exception messages.

    Parameters
    ----------
    error : Exception
        HTTP client failure.

    Returns
    -------
    str
        Registered public error code.
    """
    cause = error
    while cause is not None:
        if isinstance(cause, ssl.SSLError):
            return "configuration_error"
        cause = cause.__cause__
    if isinstance(error, (httpx.InvalidURL, httpx.UnsupportedProtocol, httpx.LocalProtocolError, httpx.ProxyError)):
        return "configuration_error"
    if isinstance(error, httpx.TimeoutException):
        return "backend_timeout"
    if isinstance(error, httpx.ConnectError):
        return "connection_failed"
    if isinstance(error, (httpx.NetworkError, httpx.RemoteProtocolError)):
        return "backend_unavailable"
    return "invalid_backend_response"


class MockClient:
    """Connect a verified MCP user to the mock's independent credential boundary.

    Parameters
    ----------
    url : str
        HTTP or HTTPS backend URL without embedded credentials.
    service_key : str
        MCP-to-mock service credential.
    user_credentials : dict of str to str
        Verified user IDs mapped to mock-specific tokens.
    transport : httpx2.AsyncBaseTransport or None, optional
        Injectable transport for controlled failure tests.
    deadline : float, optional
        Total time budget for one operation, including retries.
    """

    def __init__(self, url, service_key, user_credentials, transport=None, deadline=10.0):
        """Validate configuration without exposing credential values in errors."""
        try:
            target = httpx.URL(url)
            valid = (target.scheme in {"http", "https"} and target.host and not target.userinfo
                     and not target.query and not target.fragment and math.isfinite(deadline) and deadline > 0)
        except (httpx.InvalidURL, TypeError, ValueError):
            valid = False
        if not valid:
            raise ValueError("Configure an HTTP(S) backend URL without credentials and a positive finite deadline.")
        self.url = str(target)
        self.service_key = service_key
        self.user_credentials = dict(user_credentials)
        self.transport = transport
        self.deadline = deadline

    @staticmethod
    def validate_result(user_id, method, path, body, payload, parameters):
        """Validate response shape and operation identity before exposing backend data.

        Parameters
        ----------
        user_id, method, path : str
            Verified caller and application-defined operation.
        body : dict
            Parsed backend response.
        payload : dict
            Original request JSON, if present.
        parameters : dict
            Requested pagination values, if present.

        Returns
        -------
        dict
            Validated result with only declared response fields.
        """
        if not isinstance(body, dict):
            raise ValueError("Expected an object.")
        if method == "GET" and path == "/access":
            result = AccessResult.model_validate(body, strict=True)
            if result.user_id != user_id:
                raise ValueError("Mismatched identity.")
        elif method == "GET" and path in {"/tickets", "/tickets/overview"}:
            listing = path == "/tickets/overview"
            result_model = TicketListResult if listing else SearchResult
            result = result_model.model_validate(body, strict=True)
            if len(result.items) > result.page_size or len(result.items) > result.total:
                raise ValueError("Invalid pagination.")
            expected_count = max(0, min(result.page_size, result.total - (result.page - 1) * result.page_size))
            if (len(result.items) != expected_count
                    or result.has_more != (result.page * result.page_size < result.total)
                    or result.page != parameters.get("page", 1)
                    or result.page_size != parameters.get("page_size", 50 if listing else 5)):
                raise ValueError("Inconsistent pagination.")
        elif method == "GET" and path.startswith("/tickets/"):
            result = Ticket.model_validate(body)
            if result.id.casefold() != path.removeprefix("/tickets/").casefold():
                raise ValueError("Mismatched ticket.")
        elif path.startswith("/approvals/") and method in {"GET", "POST"}:
            result = ApprovalState.model_validate(body, strict=True)
            if result.approval_id != path.split("/")[2]:
                raise ValueError("Mismatched approval.")
            if method == "POST" and result.status != {"approve": "approved", "decline": "declined"}.get(payload.get("decision")):
                raise ValueError("Mismatched approval decision.")
        elif method == "POST":
            creating = path == "/tickets"
            operation = "create_ticket" if creating else "add_comment"
            request_model = CreateTicket if creating else AddComment
            expected = request_model.model_validate(payload).model_dump(exclude={"approval_id"})
            if not creating:
                expected["ticket_id"] = path.split("/")[2].upper()
            if body.get("status") in {"pending", "approved"}:
                result = WriteProposal.model_validate(body, strict=True)
                if result.operation != operation or result.arguments != expected:
                    raise ValueError("Mismatched proposal.")
                if payload.get("approval_id") is not None:
                    raise ValueError("Execution returned an uncompleted proposal.")
            else:
                result_model = CreatedTicketResult if creating else AddedCommentResult
                result = result_model.model_validate(body)
                if result.approval_id != payload.get("approval_id"):
                    raise ValueError("Missing or mismatched approval.")
                if creating:
                    if (result.ticket.requester_id != user_id or result.ticket.visibility != "internal"
                            or result.ticket.title != expected["title"] or result.ticket.description != expected["description"]):
                        raise ValueError("Mismatched ticket owner or visibility.")
                elif (result.comment.author_id != user_id or result.ticket_id.upper() != expected["ticket_id"]
                      or result.comment.body != expected["body"]):
                    raise ValueError("Mismatched comment target or author.")
        else:
            raise ValueError("Unsupported operation.")
        return result.model_dump(mode="json")

    @staticmethod
    def http_error(response, body):
        """Select a registered error without trusting backend messages or actions.

        Parameters
        ----------
        response : httpx2.Response
            Fully read backend error response.

        Returns
        -------
        tuple
            Public code and rollback confirmation.
        """
        try:
            error = body.get("error", {})
            code = error.get("code")
            if code in ERRORS and ERRORS[code].status == response.status_code:
                return code, error.get("outcome") == "not_applied"
        except (ValueError, AttributeError, TypeError):
            pass
        status = response.status_code
        codes = {400: "invalid_arguments", 401: "authentication_failed", 403: "access_denied",
                 404: "not_found_or_inaccessible", 405: "method_not_allowed", 408: "backend_timeout",
                 409: "approval_state_conflict", 410: "not_found_or_inaccessible", 412: "precondition_failed",
                 413: "request_too_large", 414: "request_too_large", 415: "configuration_error",
                 422: "invalid_arguments", 425: "backend_unavailable", 428: "precondition_failed",
                 429: "rate_limited", 500: "backend_unavailable", 501: "backend_not_supported",
                 502: "backend_unavailable", 503: "backend_unavailable", 504: "backend_timeout",
                 505: "backend_not_supported", 507: "storage_unavailable"}
        code = "configuration_error" if 300 <= status < 400 else codes.get(status, "internal_error" if status >= 500 else "request_rejected")
        return code, False

    async def request(self, user_id, method, path, *, session_id=None, **kwargs):
        """Trace a logical backend operation, including its bounded retries.

        Parameters
        ----------
        user_id, method, path : str
            Verified identity and application-defined backend operation.
        session_id : str or None, optional
            Trusted write context.
        **kwargs : dict
            Validated parameters or proposal payload.

        Returns
        -------
        dict
            Validated result with original approval and idempotency semantics.
        """
        with observation(
            "backend.request", input={"params": kwargs.get("params"), "body": kwargs.get("json")},
            metadata={"request_id": REQUEST_ID.get(), "method": method, "path": path,
                      "acting_user": user_id, "max_attempts": 3},
        ) as span:
            try:
                result = await self._request(user_id, method, path, session_id=session_id, **kwargs)
            except ToolFailure as error:
                span.update(output=error.error.payload)
                raise
            span.update(output=result)
            return result

    async def _request(self, user_id, method, path, *, session_id=None, **kwargs):
        """Execute a bounded operation and return an actionable MCP error on failure.

        Parameters
        ----------
        user_id : str
            Verified identity, never selected by the model.
        method, path : str
            Application-defined backend operation.
        session_id : str or None, optional
            Trusted conversation context for proposals and writes.
        **kwargs : dict
            Query parameters or validated JSON payload.

        Returns
        -------
        dict
            Validated backend data.

        Raises
        ------
        ToolFailure
            Minimal public error with a write outcome only when relevant.
        """
        request_id = REQUEST_ID.get() or str(uuid4())
        attempts = 0
        payload = kwargs.get("json") or {}
        write = method == "POST" and path.startswith("/tickets") and bool(payload.get("approval_id"))
        safe_retry = method in {"GET", "HEAD"} or (
            method == "POST" and session_id and (path == "/tickets" or path.endswith("/comments"))
        ) or (method == "POST" and session_id and path.startswith("/approvals/"))
        uncertain = False
        in_flight = False

        def failure(code, retry_after=None):
            """Build the final safe error using accumulated write uncertainty."""
            outcome = "unknown" if uncertain else "not_applied" if method == "POST" else None
            return ToolFailure(PublicError(code, request_id=request_id, attempts=attempts,
                                           outcome=outcome, retry_after=retry_after))

        credential = self.user_credentials.get(user_id)
        if not credential:
            raise failure("authentication_failed")
        headers = {"X-Mock-Service-Key": self.service_key, "Authorization": f"Bearer {credential}",
                   "X-Request-Id": request_id}
        if session_id is not None:
            headers["X-Session-Id"] = session_id
        now = asyncio.get_running_loop().time()
        expires = min(now + self.deadline, OPERATION_DEADLINE.get() or math.inf)
        budget = expires - now
        if budget <= 0:
            raise failure("deadline_exceeded")
        try:
            async with asyncio.timeout(budget):
                async with httpx.AsyncClient(
                    base_url=self.url, headers=headers, transport=self.transport,
                    timeout=min(3.0, budget), follow_redirects=False, trust_env=False,
                ) as client:
                    for attempts in range(1, 4):
                        retry_after = None
                        code = "internal_error"
                        try:
                            in_flight = True
                            async with traced_attempt(client, method, path, attempts, **kwargs) as response:
                                chunks = []
                                size = 0
                                async for chunk in response.aiter_bytes():
                                    size += len(chunk)
                                    if size > MAX_RESPONSE_BYTES:
                                        uncertain |= write
                                        raise failure("invalid_backend_response")
                                    chunks.append(chunk)
                                try:
                                    data = json.loads(b"".join(chunks))
                                except (ValueError, UnicodeDecodeError):
                                    data = None
                                in_flight = False
                                if 200 <= response.status_code < 300:
                                    try:
                                        result = self.validate_result(user_id, method, path, data, payload, kwargs.get("params") or {})
                                    except (ValueError, TypeError, KeyError, ValidationError):
                                        uncertain |= write
                                        raise failure("invalid_backend_response") from None
                                    return result
                                code, not_applied = self.http_error(response, data)
                                uncertain |= write and response.status_code >= 500 and not not_applied
                                retry_after = retry_after_seconds(response.headers.get("Retry-After"))
                                retry = response.status_code in RETRY_STATUSES and ERRORS[code].is_retryable
                        except ToolFailure:
                            raise
                        except (httpx.RequestError, httpx.InvalidURL) as error:
                            in_flight = False
                            code = transport_error_code(error)
                            uncertain |= write and not isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
                            retry = ERRORS[code].is_retryable
                        logger.warning("backend_failure code=%s request_id=%s attempt=%d", code, request_id, attempts)
                        if attempts == 3 or not retry or not safe_retry:
                            raise failure(code, retry_after)
                        remaining = expires - asyncio.get_running_loop().time()
                        if retry_after is not None and retry_after >= remaining:
                            raise failure(code, retry_after)
                        base = 0.1 * 2 ** (attempts - 1)
                        delay = base + random.uniform(0, base * 0.25)
                        with observation(
                            "backend.retry_wait", metadata={
                                "attempt": attempts, "next_attempt": attempts + 1,
                                "error_code": code, "backoff_seconds": max(delay, retry_after or 0),
                                "retry_after_seconds": retry_after, "idempotent_retry": safe_retry,
                                "write_outcome": "unknown" if uncertain else "not_applied" if write else None,
                            },
                        ):
                            await asyncio.sleep(max(delay, retry_after or 0))
        except ToolFailure:
            raise
        except TimeoutError:
            uncertain |= write and in_flight
            raise failure("deadline_exceeded") from None
        except asyncio.CancelledError:
            logger.info("backend_cancelled request_id=%s", request_id)
            raise
        except Exception:
            uncertain |= write and in_flight
            raise failure("internal_error") from None
        raise failure("internal_error")
