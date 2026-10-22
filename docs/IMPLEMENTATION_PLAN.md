# Completion plan

The user explicitly authorized completing this batch on 2026-10-21. Keep the
existing create_agent ReAct loop, MCP/API authorization and durable backend
idempotency. Do not commit or push this batch without a new instruction.

## Observable acceptance criteria

- A model-selected create/comment call stages one exact backend proposal and
  interrupts the graph. No ticket changes before a later explicit decision.
- Approve resumes that action; decline never executes it. Decisions bind user,
  session, proposal and arguments. Restart, duplicate approval, changed access
  and uncertain write outcomes cannot duplicate a mutation.
- A clearly stated personal operating system is available in another session
  of the same user, expires after 30 days, and can be deleted. Blocked input,
  ticket instructions and another user cannot populate or read it.
- The browser displays exact proposed arguments and sends approve/decline with
  the proposal ID. Reloading the same authenticated session restores the review.
- The running mock can enable finite rate limits, server errors, slow responses
  and lost committed-write responses through an authenticated operator switch.
- A locked container build starts mock, MCP and chat with persistent data and
  health checks. Local Langfuse remains separate infrastructure.

## Implementation order

- [x] Durable graph interruption and authenticated application approval bridge.
- [x] User fact storage, expiry, deletion and transient model context.
- [x] Browser proposal display, approval decisions and restart recovery.
- [x] Runtime fault switch and controlled failure tests.
- [x] Application Dockerfile, Compose startup and clean database initialization.
- [x] Protocol tests, model evals where credentials permit, documentation and
      recorded limitations. Preserve historical evaluations unchanged.

## Verification scope

Test creation and comments; stop-before-write; decline; restart; repeated
approval; stale/mismatched decisions; revoked permissions; timeout after commit;
user/session isolation; fact overwrite/expiry/deletion; UI request construction;
finite fault recovery and exhausted failures. Run the existing Python and
frontend suites. Container validation must distinguish configuration validation,
image build and an actual running smoke test.
