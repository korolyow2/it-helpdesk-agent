# MCP tool contracts

## Boundaries and identity

```text
MCP client -> FastMCP -> mock HTTP API -> SQLite
```

The runtime exposes Streamable HTTP at `/mcp` in stateless HTTP mode. The client
authenticates with a user-specific MCP bearer token. FastMCP verifies it against
the local token configuration and checks the user's current Helpdesk membership
through the mock's `/access` endpoint. Failure to validate access fails closed.
Token verification is local. Live membership is checked separately when listing
or calling tools, so a backend outage remains an availability error rather than
being reported as an invalid MCP token.

The MCP server uses a separate service key and a mock-specific credential for
the verified user. It does not forward the incoming MCP token or trust a user ID
in tool arguments. The mock derives identity from its own credential mapping and
reads permissions from SQLite for each operation. Scope checks never grant more
rights than the backend permits.

The application sets `X-Session-Id` on MCP requests. For writes it must be 1 to
128 ASCII letters, digits, underscores, periods, colons, or hyphens, starting with
a letter or digit. This context is bound together with the verified user ID;
knowledge of another user's session or approval ID does not grant access.

Static tokens are a local-development simplification. No OAuth flow, production
identity provider, or chat authentication UI is implemented in this step.

## Tools

| Tool | Arguments | Result |
| --- | --- | --- |
| `list_tickets` | `page=1`, `page_size=50` | Accessible IDs, titles, statuses, `total`, `page`, `page_size`, `has_more` |
| `search_tickets` | `query`, `page=1`, `page_size=5` | Accessible summaries, `total`, `page`, `page_size`, `has_more` |
| `get_ticket` | `ticket_id` | Full accessible ticket and comments |
| `create_ticket` | `title`, `description`, optional `approval_id` | Pending proposal or completed ticket result |
| `add_comment` | `ticket_id`, `body`, optional `approval_id` | Pending proposal or completed comment result |

There is no tool to grant approval, alter permissions, select another user, or
execute arbitrary SQL or HTTP requests. Read-only annotations are descriptive
hints; backend code enforces the actual authorization and write gate.

The agent validates generated arguments against the discovered tool JSON schema
before an MCP call. The server and mock still perform their own validation.
An `invalid_arguments` result permits one changed-argument correction of the
same tool per user turn, within existing execution budgets. Repeating invalid
arguments or receiving any other error blocks further tool execution. The
error payload remains minimal; argument values are not echoed in its message.

### Initial overview

The first model call precedes any MCP connection. Initially only the local
`discover_tools` utility is available. When the model selects it, the adapter
opens MCP and obtains live authorized read schemas. It then offers only
`list_tickets`; after that succeeds, the model can select search and detail tools.
Discovery returns only the next available capability name, without ticket data.
A denied user never receives ticket tools. The registry is recreated per user
turn; every MCP operation still independently rechecks authorization.
A code guard rejects attempts to bypass this dependency, including invented
calls to currently unoffered tools. A listing error reaches the model and ends
tool use for that turn. No ticket data is prefetched before the model.

All six tool calls and six model calls are bounded by a shared request deadline.
Authentication and discovery remain independent code-enforced checks. Old ticket
evidence is hidden until a fresh overview confirms access to every historical
ID; no automatic historical detail reads are performed. Pages are ordered by ID,
with visibility filtered before counts and pagination. Only id, title, and status
are returned. A page is incomplete when has_more is true.

### Search

After the initial overview, when the user knows the ID, call `get_ticket`. IDs are case-insensitive
and surrounding whitespace is removed. `search_tickets` also recognizes IDs such
as `TKT-001`, `check tkt-001`, or `TKT-001, TKT-002`. When IDs occur in a query,
only those exact IDs are searched; an unknown or hidden ID does not fall back to
unrelated keyword matches. Search returns an empty result for both.

Without IDs, search matches literal alphanumeric keyword substrings in titles and descriptions
with OR semantics. Title matches weigh three times as much as description matches.
Use short technical queries, then read candidate details rather than treating
the highest-ranked match as proof that two incidents are the same.

All terms are bound as SQL parameters. Visibility is filtered before scoring,
counting, and pagination. Equal scores are ordered by ticket ID. No matches is a
successful response with `items: []`; invalid arguments and backend errors are
not empty searches. The MCP search schema permits page numbers from 1 to 1,000;
listing permits 1 to 1,000,000. Page size ranges from 1 to 50.

This is deliberately lexical search, without embeddings or automatic synonym
handling. Lowercasing follows SQLite's default behavior, suitable for the current
English fixtures. Offset pages are stable only while relevant data is unchanged.

### Writes and approval

1. Call a write tool without `approval_id`. The mock validates access and saves
   a `pending` proposal; no ticket or comment changes.
2. Return the exact proposal to the user and stop. One pending or approved action
   is allowed per user/session. Repeating the same pending proposal returns its
   existing ID. Different arguments require declining the old proposal first.
3. An explicit human decision arrives through the application-only mock endpoint
   `POST /approvals/{approval_id}/decision` with `{"decision":"approve"}` or
   `{"decision":"decline"}`. It requires the same user's credentials and session.
   Recording the decision does not execute the write.
4. After approval, call the same write tool with the exact normalized arguments
   and the returned `approval_id`. The mock checks ownership, session, current
   rights, operation, payload, and approval state before mutating a ticket.
5. The mutation and the `completed` result commit in one SQLite transaction.
   Retrying the same approval returns that result without a duplicate mutation.

For example, the first call can return:

```json
{
  "approval_id": "a-generated-uuid",
  "status": "pending",
  "operation": "add_comment",
  "arguments": {
    "ticket_id": "TKT-001",
    "body": "The issue also occurs after restarting."
  }
}
```

Creation always produces an `open`, `internal` ticket owned by the authenticated
user. Comments record that user's ID and a server-generated timestamp. Neither
operation accepts requester, author, role, status, or visibility overrides.

`GET /approvals/{approval_id}` restores proposal state after restart. Both that
endpoint and the decision endpoint require the mock service key, mock user token,
and `X-Session-Id`. They are not MCP tools. The chat application records an
explicit next-message decision through these endpoints and resumes the native
LangGraph interrupt. The model-facing write schemas omit approval_id; code
injects the stored ID only after checking the exact user/session/action binding.
The tool node's durable binding prevents proposal restaging when it replays.
Decline returns a processed rejection without calling the MCP execution tool.
Completion and uncertain-write receipts are application-owned, without an extra
model generation. See [the implemented flow](../README.md#approval-and-recovery).

## Failures

Membership and data calls share one correlation ID and a 10-second backend-work
budget per MCP operation. Each upstream call makes at most three attempts.
The agent's MCP transport timeout is twelve seconds, allowing the backend
deadline to return a typed error before the transport expires. Retry policy
distinguishes temporary faults from authentication, authorization,
validation, approval, configuration, and storage failures. See
[error handling and recovery](ERRORS.md) for exact categories and actions.

Successful HTTP responses are checked against result schemas, pagination,
identity, target, proposal arguments, and approval identity. Invalid or oversized
responses fail closed. Raw exception text, credentials, rejected values, and
backend diagnostics are excluded from public errors.

Tool failures use MCP `isError: true` with JSON in the text content. Discovery
failures use a JSON-RPC error with the same minimal error in its data. Invalid MCP
credentials are rejected by the SDK's HTTP authentication layer before tools run.
Local Langfuse tracing is configured; see `OBSERVABILITY.md`. The eval runner
separates deterministic checks from real-model trials and reports missing
credentials and unimplemented capabilities explicitly; see `EVALUATION.md`.
