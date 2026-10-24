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
- Answer text reaches the browser as the model generates it. Thinking and
  tool-call chunks are excluded; text from a known tool-call message is hidden.
- The renderer displays received fragments word by word, with a 40 ms pause
  between words in each fragment. This
  also animates ready-made application replies, rather than implying that a
  model generated those replies token by token. Hidden tabs and reduced-motion
  preferences skip the animation. Errors remain immediate.
- `done` confirms that the received deltas match the final answer and returns
  the completed request ID. A failed or truncated generation ends with `error`;
  text already shown remains partial. A `text_reset` event supersedes narration
  when the model selects another tool; the next text fragment overwrites the
  draft. Completion validates only the current draft. No tool payload is sent.
- A separate authenticated GET `/chat/requests/{request_id}/tools` fills the tool
  panel with names and statuses. No ticket payloads or arguments enter that
  panel. It is collapsed by default and follows the confirmation card.
  Summaries expire after 128 completed requests or a service restart.
- Requests are not automatically replayed after a failure. The user can send a
  new message; identity/session controls are locked while a request is active.
- One status spinner indicates a pending reply. The send button is hidden until
  completion, while the input remains available for drafting the next message.
- Sending before Connect shows an error without calling the API. Failed login,
  HTTP 401, or an authentication error in the stream clears the connection.
  Every exit closes the component stream and restores the controls. Application
  errors use a separate message role to avoid Deep Chat 2.5.1's error-only
  stream finalization bug; they do not count as completed API replies.
- Blocked requests receive a static reply and leave server conversation history
  unchanged. Their visible browser bubbles are never sent back as history.
- A separate authenticated GET `/chat/sessions/{session_id}/approval` restores
  the exact pending proposal. Buttons send approve/decline with its ID in the
  next request. Pending review values render as text; stale IDs cannot approve
  another action. Completed/declined outcomes are distinct from tool errors.
- Chat asks whether to create the ticket or add the comment in plain language,
  showing its exact content. The card uses readable labels rather than operation
  names or schema keys. Approval IDs remain application metadata.
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

The 24 JavaScript tests cover arbitrary chunk boundaries, split UTF-8/CRLF,
partial failures, malformed events, missing completion, mismatched answers,
approval binding, authentication failures, controller cleanup and recovery,
progressive delivery before completion, replacement of superseded drafts,
incremental rendering of batched text, Unicode preservation and motion opt-out.

Browser checks on 2026-10-23 used a local synthetic service to verify a missing
connection, invalid credentials, an authentication error before the first token,
the pending-reply indicator, preserved drafts, and restored sending controls.

Earlier browser checks used synthetic responses to verify the tool panel,
blocked replies, partial errors, and recovery on a later request. A separate
check used the real graph, MCP, and mock with a scripted model: approval added
one comment, and decline added none. An earlier container smoke test used the
real Sonnet provider. These historical checks do not replace a demonstration
of the current application or a fresh model evaluation.
