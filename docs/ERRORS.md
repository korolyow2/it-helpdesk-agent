# Error handling and recovery

## Error contract

The mock returns `{"error": {...}}` with an appropriate HTTP status. MCP tool
failures return `isError: true` and a JSON text payload such as:

```json
{
  "is_error": true,
  "code": "access_denied",
  "message": "This user does not have Helpdesk access.",
  "is_retryable": false
}
```

These four fields form the normal agent-facing error. `is_retryable` describes
the failure class; it does not authorize indefinite retries after the client's
bounded policy has finished. The upcoming agent uses `code` to choose recovery.

Only situation-specific data is added: `retry_after_seconds` for a rate limit
with a valid wait, and `outcome` for a write failure. Read errors omit `outcome`.
There are no `next_action`, `fields`, `write_recovery`, `attempts`, or `request_id`
fields in the agent payload. Attempt counts and correlation IDs stay internal;
HTTP responses also expose correlation through `X-Request-Id`.

## Categories and actions

| Codes | Action after the call fails |
| --- | --- |
| `authentication_required`, `authentication_failed` | restore valid user credentials; never choose another user |
| `service_authentication_failed`, `configuration_error` | fix service credentials, URL, TLS, or protocol; no automatic retry |
| `access_denied` | stop; do not probe through other tools |
| `not_found_or_inaccessible` | explain that the ticket is unavailable without revealing whether it exists |
| `invalid_arguments` | one corrected call of the same tool per user turn; ask for missing information rather than inventing values |
| `session_required` | the application must supply a valid trusted session header |
| `request_too_large` | reduce the supplied content |
| `approval_required` | show the saved proposal and wait for the user's decision |
| `approval_declined` | stop the action; never execute or automatically resubmit it |
| `approval_mismatch` | changed arguments need a new proposal and decision |
| `pending_approval_exists` | resolve the existing action before proposing another |
| `approval_not_found`, `approval_state_conflict` | reload state through the application in the same user/session context |
| `data_conflict`, `precondition_failed` | read the current state; changed actions need fresh approval |
| `rate_limited` | respect the backend wait; explain that the request is delayed |
| `connection_failed`, `backend_timeout`, `backend_unavailable`, `database_busy`, `deadline_exceeded` | explain the temporary failure after bounded retries; do not report success |
| `storage_unavailable`, `database_error` | repair storage or investigate the database; no blind retries |
| `invalid_backend_response`, `backend_not_supported` | investigate the backend contract or compatibility |
| `method_not_allowed`, `request_rejected`, `internal_error` | stop; the application can correlate internal logs for investigation |
| `execution_limit_exceeded` | Stop after the configured agent execution budget |
| `history_access_changed` | Start a new session without previously accessible ticket content |
| `context_limit_exceeded` | Narrow the request or use a new session; the current turn is not silently truncated |
| `incomplete_model_output` | Treat the truncated answer as incomplete; do not report success |
| `interrupted_read` | An earlier read has no confirmed result; do not report success |
| `unknown_tool` | refresh the available tool catalog |

SQLite locks are temporary; full, read-only, corrupt, inaccessible, and I/O-failing
storage requires operator attention. Constraint violations roll back the
transaction. Unexpected exceptions have a safe fallback without their raw text.
Framework-level malformed JSON-RPC and HTTP authentication errors are handled by
the MCP SDK before the application tool error contract applies.

## Bounded retries

- Each upstream call makes at most three attempts within a shared 10-second MCP
  operation budget; individual network phases have at most three seconds.
- Retry temporary connection/read/write/network failures and HTTP 408, 425, 429,
  500, 502, 503, and 504, provided the recognized application error is retryable.
  A known permanent application error overrides a generic transient HTTP status.
- Use exponential backoff of 0.1 and 0.2 seconds plus up to 25 percent jitter.
  A valid `Retry-After` (finite nonnegative seconds or HTTP date) sets a minimum
  wait. If it exceeds the remaining budget, return immediately with that delay
  rather than retrying early or holding the request open.
- Do not retry authorization, invalid input, approval conflicts, unsupported
  protocol, TLS/certificate errors, invalid responses, or permanent storage faults.
  Redirects are not followed. Caller cancellation is propagated without retry.
- Writes are retried only through the application's durable deduplication path.
  Active proposals deduplicate by user/session/exact action; approved writes use
  their persisted approval ID. A mutation and its replay result commit together.

## Unknown write outcomes

For write errors, `outcome` is `not_applied` when no ticket write is confirmed
to have been attempted or the backend confirms rollback/rejection, and `unknown`
when a write may have committed but its result is unavailable. A later rejection
cannot erase uncertainty from an earlier lost response.

For `unknown`, restore access if needed and retry only the exact action with the
same `approval_id`. Never report success or definite failure, create a replacement
action, or use a new approval ID to bypass uncertainty. A valid replay returns the
stored completed result without another mutation. These are application rules;
recovery instructions are not duplicated in each error payload.

The HTTP client implements retries, deadlines, validation, and safe error results.
The read-only agent validates arguments against the MCP schema before execution
and allows one changed-argument correction of the same tool after
`invalid_arguments`. Repeating unchanged arguments consumes that opportunity
without reaching MCP. A second argument error or any other tool error blocks
further tools. `is_retryable` describes transport recovery; argument correction
does not change it to true. Interactive credential/state recovery and approval actions remain upcoming;
there is no implemented OAuth refresh or autonomous operator intervention.

## Verification and limits

Deterministic tests cover HTTP and transport classifications, retry limits,
Retry-After parsing, shared deadlines, cancellation, live access revocation,
malformed/oversized responses, secret redaction, database faults, approval gates,
transaction rollback, and lost committed-write responses. Existing tests also
cover restarts, repeated approvals, concurrency, and user isolation.

Failure injection currently uses test transports and controlled database faults.
The interactive runtime failure switch, Langfuse traces, and real-model recovery
evals remain future work. This is a local mock contract, not a guarantee about
an arbitrary external helpdesk's idempotency or error semantics.


## Chat streaming errors

POST /chat/stream returns answer text through text_delta events and confirms
completion through done. Failures after streaming has started become a terminal
error event with the minimal public error payload and no done event. Partial text
is not a completed answer. Authorization/body errors before the stream starts
use a normal non-2xx JSON response. Internal provider and tool data is not sent
as public SSE events.

### Empty provider refusal

Anthropic may return no text with `stop_reason="refusal"`. This is handled as
`model_refused` (HTTP 422, `is_retryable=false`), not an internal bug or a
successful empty answer. No model substitution or unchanged-request retry is
performed. A real encoded synthetic request reproduced this provider behavior;
the original failed trial and follow-up verification remain in eval evidence.
See [Anthropic stop reasons](https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons).
