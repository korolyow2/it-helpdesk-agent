# Helpdesk web interface

Plain HTML, CSS and JavaScript with the locally vendored Deep Chat 2.5.1 web
component. FastAPI serves the page and assets; no Node.js build or runtime CDN
connection is required. JavaScript modules are served through HTTP, not file URLs.

## Run

Start the mock API and MCP server using the root README, then start the chat API
with the same configured environment:

```sh
uv run uvicorn helpdesk.api:from_env --factory --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/`. Enter the configured user ID and that user's
Helpdesk bearer token from `MCP_USER_TOKENS`, not an Anthropic API key. Click
Connect and send a message. Credentials stay in page memory; no browser storage
is enabled. A new session gets a random session ID. Reconnecting clears visible
browser messages, but the server retains accepted history for that user/session.
Changing the user or session cannot replay the previous browser's history.

## Behavior

- POST `/chat/stream` sends only the newest message plus user/session identifiers.
- The outer LangGraph workflow screens input before the persistent ReAct graph.
- Model fragments are held until the final message is validated, excluding
  text emitted before a tool call. This delays first visible text until generation
  completes; it is SSE delivery rather than live provider-token delivery.
- Only final answer `text_delta` events enter the chat. `done` confirms the exact text
  and returns the completed request ID. Incomplete streams remain errors.
- A separate authenticated GET `/chat/requests/{request_id}/tools` fills the tool
  panel with names and statuses. No ticket payloads or arguments enter that
  panel. Summaries expire after 128 completed requests or a service restart.
- Requests are not automatically replayed after a failure. The user can send a
  new message; identity/session controls are locked while a request is active.
- Blocked requests receive a static reply and leave server conversation history
  unchanged. Their visible browser bubbles are never sent back as history.
- A separate authenticated GET `/chat/sessions/{session_id}/approval` restores
  the exact pending proposal. Buttons send approve/decline with its ID in the
  next request. Pending review values render as text; stale IDs cannot approve
  another action. Completed/declined outcomes are distinct from tool errors.
- An already approved action offers only an explicit retry of the same ID.
  A previously unknown outcome can be confirmed by the review endpoint; a
  completed review is displayed without enabled mutation controls.

## Dependency

`vendor/deep-chat-2.5.1.bundle.js` is the pinned upstream ES module:

- Source: https://unpkg.com/deep-chat@2.5.1/dist/deepChat.bundle.js
- SHA-256: `6844eaf354e19d50dcdd8ee78968d6ae32ccd4b9f15a883902d60e93d69caf84`
- Upstream: https://github.com/OvidijusParsiunas/deep-chat
- MIT license retained as `vendor/deep-chat.LICENSE`.

Keep this component's version and checksum explicit when updating it. Its
custom handler adapts our own answer stream rather than exposing provider
credentials or changing the server's conversation/authorization contract.

## Verification

```sh
uv run python -m pytest backend/tests/agent/test_frontend.py backend/tests/agent/test_input_guardrail.py
node --test frontend/tests/*.test.mjs
```

The JavaScript parser tests cover arbitrary chunk boundaries, split UTF-8/CRLF,
partial failures, malformed events, missing completion and mismatched answers.
The browser smoke test used a temporary synthetic-response server with no
provider API calls. It verified successful streaming, the separate tool panel,
a static blocked reply, a partial-error display and a successful next request.
See `docs/evidence/frontend-smoke.png`. These checks are not a new real-model
quality evaluation or evidence of conversational approved writes.

The current completion browser check restored two real graph interrupts through
the authenticated UI. Approve added one comment; decline added none. The fixture
used a scripted model with the real create_agent/MCP/mock stack, not a paid model.
[Browser evidence](../docs/evidence/completion-browser.json) and the pending,
approved and declined screenshots under `docs/evidence/completion-review-*.png`
record the result. The separate container smoke uses the real Sonnet provider.
