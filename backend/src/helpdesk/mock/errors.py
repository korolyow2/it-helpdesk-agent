"""Return safe HTTP errors without leaking database details or request values."""

import logging
import sqlite3
from uuid import UUID, uuid4

from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from helpdesk.errors import PublicError, REQUEST_ID


logger = logging.getLogger(__name__)


def install_error_handlers(app):
    """Install public error responses and request correlation on the mock API.

    Parameters
    ----------
    app : fastapi.FastAPI
        Mock application to configure.
    """

    @app.middleware("http")
    async def correlate(request, call_next):
        """Associate errors and response headers with a valid correlation ID."""
        try:
            request_id = str(UUID(request.headers.get("X-Request-Id", "")))
        except ValueError:
            request_id = str(uuid4())
        request.state.request_id = request_id
        token = REQUEST_ID.set(request_id)
        try:
            response = await call_next(request)
            response.headers["X-Request-Id"] = request_id
            return response
        finally:
            REQUEST_ID.reset(token)

    def response(request, error, headers=None):
        """Log only safe metadata and serialize the public failure."""
        if request.method in {"GET", "HEAD"}:
            error.payload.pop("outcome", None)
        else:
            error.payload.setdefault("outcome", "not_applied")
        logger.warning("helpdesk_error code=%s request_id=%s", error.code, error.request_id)
        return JSONResponse({"error": error.payload}, status_code=error.spec.status,
                            headers={"X-Request-Id": error.request_id, **(headers or {})})

    @app.exception_handler(PublicError)
    async def public_error(request, error):
        """Serialize an expected application failure."""
        headers = {"WWW-Authenticate": "Bearer"} if error.code == "authentication_failed" else None
        return response(request, error, headers)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        """Report invalid arguments without echoing rejected values or the body."""
        return response(request, PublicError("invalid_arguments"))

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        """Normalize routing and framework HTTP failures."""
        codes = {400: "invalid_arguments", 401: "authentication_failed", 403: "access_denied",
                 404: "not_found_or_inaccessible", 405: "method_not_allowed", 413: "request_too_large",
                 422: "invalid_arguments"}
        return response(request, PublicError(codes.get(error.status_code, "request_rejected")))

    @app.exception_handler(sqlite3.DatabaseError)
    async def database_error(request, error):
        """Distinguish temporary locks from permanent storage and integrity faults."""
        sqlite_code = getattr(error, "sqlite_errorcode", 0) & 0xFF
        if sqlite_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            code = "database_busy"
        elif sqlite_code in {sqlite3.SQLITE_READONLY, sqlite3.SQLITE_FULL, sqlite3.SQLITE_IOERR,
                             sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}:
            code = "storage_unavailable"
        elif isinstance(error, sqlite3.IntegrityError):
            code = "data_conflict"
        else:
            code = "database_error"
        return response(request, PublicError(code))

    @app.exception_handler(ResponseValidationError)
    async def invalid_response(request, error):
        """Hide malformed stored data and response validation details."""
        return response(request, PublicError("internal_error", outcome="unknown"))

    @app.exception_handler(Exception)
    async def unexpected_error(request, error):
        """Fail safely for unclassified exceptions without exposing their text."""
        request_id = getattr(request.state, "request_id", None)
        return response(request, PublicError("internal_error", request_id=request_id, outcome="unknown"))
