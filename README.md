# IT Helpdesk agent

A local assistant that finds support tickets and prepares ticket updates.
It shows the exact proposed ticket or comment, waits for a later user approval,
and saves the approved change once.

## Quick setup instructions

### Start the application

You need Docker with Compose and an Anthropic API key. Model calls use the paid
Anthropic API. No AWS account is required. Tracing is off by default.

```sh
cp .env.example .env
```

Edit `.env` locally and replace the placeholders:

| Setting | Value |
| --- | --- |
| `ANTHROPIC_API_KEY` | Your Anthropic API key |
| `MCP_USER_TOKENS` | Random tokens mapped to the four existing demo user IDs |
| `MOCK_USER_TOKENS` | A different set of random tokens for those same users |
| `MOCK_SERVICE_KEY` | A separate random service key |

Keep the example's JSON format: each token maps to a user ID. Keep credentials
out of Git and use different values for MCP, mock, and service credentials.

```sh
docker compose --env-file .env up --build -d --wait
```

Open `http://127.0.0.1:8000/`. Enter a configured user ID, such as `user-001`,
and the token mapped to it in `MCP_USER_TOKENS`, then click **Connect**.
The Anthropic key stays on the server.

Try finding tickets about an IT problem, then request a new ticket or comment.
Review the proposal and choose **Approve** or **Decline**. The mock starts with
four users and eight tickets, including tickets that some users cannot access.

```sh
# Check health, restart without clearing data, or stop the application.
docker compose --env-file .env ps
docker compose --env-file .env restart mock chat
docker compose --env-file .env down
```

Named volumes preserve tickets, history, facts, and pending approvals.
**`docker compose down -v` deletes the application's stored data.**

### Add local Langfuse

You also need Python with uv to generate local Langfuse credentials:

```sh
uv run python scripts/init_langfuse.py
docker compose --env-file .env.langfuse -f compose.langfuse.yaml up -d
docker compose --env-file .env --env-file .env.langfuse up -d --wait
```

Open `http://127.0.0.1:3000/` and use the login saved in `.env.langfuse`.
Wait for the UI to open before running model evaluations. The script preserves
an existing credentials file. Application containers use
`host.docker.internal` to reach Langfuse on Docker Desktop. On native Linux,
a loopback-only listener may require connecting the containers to the Langfuse
network and setting a reachable `LANGFUSE_CONTAINER_URL`. There is no cloud fallback.

<details>
<summary>Run the Python services without containers</summary>

Use Python 3.11 or newer and uv. The container and CI use Python 3.13.
Run `uv sync --locked` from the repository root. Load `.env` in each terminal:

```sh
set -a
. ./.env
set +a
```

Initialize the mock once. For an existing database, use `--upgrade` instead of
reseeding. Pass the same database path used by the service:

```sh
uv run python -m helpdesk.mock.init_db --database "$MOCK_DATABASE"
```

Start each service in its own configured terminal:

```sh
# Terminal 1
uv run uvicorn helpdesk.mock.api:from_env --factory --host 127.0.0.1 --port 8001

# Terminal 2
uv run uvicorn helpdesk.mcp.server:from_env --factory --host 127.0.0.1 --port 8002

# Terminal 3
uv run uvicorn helpdesk.api:from_env --factory --host 127.0.0.1 --port 8000
```

The developer CLI uses the same user/session and approval flow. It trusts local
configuration; the HTTP interface verifies the caller's bearer credential.

```sh
uv run python -m helpdesk.agent.service \
  --user user-001 --session demo --message "Find tickets about VPN issues"
```

</details>

## Architecture overview

One Dockerfile builds the application image. `compose.yaml` starts three
containers from it:

| Service | Local port | Responsibility |
| --- | --- | --- |
| Chat | `8000` | Browser page, authenticated chat API, and agent |
| MCP | `8002` | Python FastMCP tools exposed at `/mcp` |
| Mock | `8001` | FastAPI ticket service, permissions, and approval records |

The agent reaches ticket data through MCP and the mock HTTP API. The chat
application has a separate control path for reading approval state and recording
human decisions. That control path is never offered to the model as a tool.

SQLite stores agent checkpoints, review bindings, and user facts separately
from the mock's tickets and approvals. Langfuse has its own Compose project and
storage services: PostgreSQL, ClickHouse, Redis, and MinIO, plus web and worker.
It can stay running while the application is rebuilt.

![IT Helpdesk agent request flow](diagrams/agent-flow.png)

`POST /chat` and `POST /chat/stream` accept `user_id`, `session_id`, `message`,
and an optional `approval_id`. Authenticated endpoints also restore the current
proposal, show completed tool summaries, and inspect or delete the user's fact.

## Agent design

### Frameworks and models

The project considered a manual LangGraph workflow, `create_react_agent`, and
a supervisor. It uses LangChain `create_agent`, backed by LangGraph, for the
ReAct loop. A small outer LangGraph routes input screening to that agent or a
fixed reply. One agent covers the task; no supervisor or handoff is needed.

The model choice followed the project requirements and observed integration
behavior. There was no comparative benchmark across model providers.

| Role | Selected model and settings |
| --- | --- |
| Main agent | Sonnet 5.5 through Anthropic; adaptive thinking, medium effort, 1,024 output tokens |
| Input classifier | Sonnet 5.5; low effort, tool-free `between_tools`, one output token |
| Offline answer judge | Opus 5.5; adaptive thinking, medium effort, 2,048 output tokens |

Development started with Sonnet 5 and later moved to Sonnet 5.5. Only classifier
and judge refusals can trigger one fallback to Opus 4.8. Invalid output and
transport failures do not switch models. The main agent has no automatic fallback.

`MODEL_PROVIDER` and `MODEL_NAME` are configurable. Only the Anthropic integration
is installed and evaluated; another provider needs its package and verification.

### Tool selection

On a fresh turn, the model runs before any MCP connection. It can answer or ask
a focused clarification without tools. When ticket access is needed, it selects
the local `discover_tools` utility. A successful `list_tickets` overview then
unlocks search, detail, and proposal tools. Capabilities are rebuilt each turn.

<a id="approval-and-recovery"></a>

### State and user approval

1. The model selects a write tool using the user's issue details.
2. MCP stages the exact proposal without changing a ticket. The application
   saves its user/session binding and pauses with LangGraph `interrupt`.
3. The page shows the proposed change. The next message must explicitly approve
   or decline it, through a button or text such as `approve` or `decline`.
4. Application code checks the owner, session, action, and arguments, records
   the decision, and resumes with `Command(resume=...)`.
5. The backend saves the approved change and its reusable result in one
   transaction. Repeated approval returns that result. Decline performs no write.

The model cannot grant approval or supply its ID. Code injects the stored ID
only after verification. Changed actions need a new proposal. An approved
outcome can be reconciled, but the decision cannot be changed to decline.

State survives restarts. If the process stops between staging and saving the
pause, recovery replays only the original saved proposal node. It does not
make a new agent model call or execute an approved write. The restored proposal
must be shown before accepting another decision.

A timeout after dispatch can leave the outcome unknown. Recovery uses the same
durable action ID; the application never invents success or a rollback.
The guarantee is one saved mutation per approved action, even if requests repeat.

### Memory and retention

| Data | What is kept, where, and for how long |
| --- | --- |
| Conversation | SQLite checkpoints, scoped by authenticated user and session; no automatic expiry |
| Review bindings | Agent SQLite records plus backend approval records; survive restart with no automatic expiry |
| Cross-session fact | One `operating_system` value, Windows, macOS, or Linux, in agent SQLite; 30-day expiry |

Only accepted, explicit first-person English OS statements are extracted.
The latest assertion replaces the previous fact and restarts its expiry.
Reading does not extend retention. Questions, hypothetical statements, quotes
about others, and role claims are excluded. Facts never grant permissions.

`GET /memory` shows the authenticated user's fact; `DELETE /memory` removes it.
Expired facts are removed on access. Deletion does not erase old conversations
or traces. Reusing another user's session name does not reveal their history.

Model input has an approximate 8,000-token budget. Old turns are omitted as
whole groups without deleting stored history. Current tool-call/result pairs
and provider signatures stay together. Old ticket evidence stays hidden until
a fresh overview confirms access. There is no automatic summarization or scratchpad.

## Tool design

Tool names describe concrete Helpdesk actions. Their English descriptions explain
when to use them, required arguments, pagination, proposal behavior, and failures.
Runtime descriptions use general rules without fixture IDs or expected eval answers.

| Tool | Purpose |
| --- | --- |
| `list_tickets` | Paginated IDs, titles, and statuses for accessible tickets |
| `search_tickets` | Paginated exact-ID or literal keyword search |
| `get_ticket` | Authorized ticket status, description, and comments |
| `create_ticket` | Stage a new internal ticket; execute only the exact approved action |
| `add_comment` | Stage a comment after reading its target; execute after approval |

Keyword search uses OR matching, with no embeddings or automatic synonyms.
Authorization is applied before counting and pagination. An inaccessible ticket
is indistinguishable from a missing one in the normal response.

### Authentication and permissions

The caller uses a user-specific MCP bearer token. FastMCP verifies it and checks
current Helpdesk membership. To call the mock, MCP uses both:

- `X-Mock-Service-Key` for service authentication.
- A separate mock bearer credential mapped to the verified user.

MCP does not forward its incoming token or accept a model-supplied user identity.
The application supplies trusted session context through `X-Session-Id`.
The backend independently checks current rights on reads, decisions, and writes.
Users without Helpdesk access receive no ticket tools.

### Errors and recovery

MCP tool failures set `isError: true` and return a small JSON payload to the agent:

```json
{
  "is_error": true,
  "code": "access_denied",
  "message": "This user does not have Helpdesk access.",
  "is_retryable": false
}
```

Rate limits may add `retry_after_seconds`; write failures may add `outcome`,
with `not_applied` or `unknown`. Request IDs and attempt counts stay in diagnostics.
Discovery failures use a JSON-RPC error; invalid MCP credentials are rejected by
HTTP authentication before a tool executes.

Arguments are checked against the discovered schema. After `invalid_arguments`,
the model can correct that same tool's arguments once. Repeated invalid arguments
or another terminal error stop tool execution for the turn. `is_retryable` describes
transport recovery; it does not give the model permission to retry indefinitely.

Backend calls allow at most three suitable attempts within 10 seconds, with
backoff and bounded `Retry-After`. MCP allows 12 seconds for that typed result
to arrive. Requests have a 60-second deadline and limits of six agent model calls
and six tool calls. Provider calls use a 30-second timeout and two SDK retries.
Writes are retried only through the durable action-ID path.

## Quality

### Cases and metrics

Correctness tests use synthetic model responses with the real graph, MCP, mock,
and databases. They check permissions, pagination, argument correction, approval,
decline, restart recovery, duplicate prevention, uncertain writes, and user isolation.
They do not measure the model's tool-selection ability.

Real-model datasets are separate:

| Dataset | Cases | Coverage |
| --- | ---: | --- |
| Earlier read-only regression set | 54 | 21 tool-selection, 11 answer-quality, and 22 adversarial/control cases |
| Completion set | 17 | 11 tool/workflow, two answer/memory, and four adversarial/control cases |

The earlier set reflects the read-only contract at that development stage.
Use the completion set for the implemented approval and write workflow.

The adversarial cases include authority spoofing, instruction injection, encoded
requests, multilingual overrides, gradual diversion, and legitimate IT controls.
Completion cases include ticket creation, comments, decline, restart, duplicate
approval, failed or uncertain writes, runtime faults, and cross-session memory.

Code graders inspect actual tool calls and database changes. The offline Opus
judge scores groundedness, helpfulness, scope adherence, instruction adherence,
and clarity. It has no application tools and cannot authorize a write.

Metrics include code pass, judge availability and pass, joint pass, guardrail
false blocks/false allows/unavailable decisions, model/tool counts, available
usage, and latency p50/p95. Judge usage is recorded separately. Missing results
are failures or unavailable measurements, never invented scores.

### Latest recorded results

| Check | Result | Scope and date |
| --- | --- | --- |
| Python correctness tests | 338 passed | Latest recorded source checks, 2026-10-22 |
| Protocol requirements | 33/33 passed | Mapped requirements and regressions, 2026-10-22 |
| JavaScript tests | 11/11 passed | Stream handling and approval binding, 2026-10-22 |
| Real-model completion | 17/17 code, judge, and joint pass | Earlier source revision, 2026-10-21 |
| Completion input screening | 30/30 correct, no false blocks or false allows | Same historical run |
| Completion agent latency | p50 9.20s; p95 16.92s | Same historical run; local experiment, not a production benchmark |

**Paid model evaluations were not rerun after the review and SQLite fixes.**
Their scores are historical evidence, not a measurement of current model quality.
The completion report has one trial per case, so it does not establish reliability
across repeated runs.
Reports, failed trials, and source/prompt/dataset hashes remain in `evals/results/`.
Native datasets and experiment scores are also recorded in local Langfuse.

Run correctness checks with Python/uv and Node.js 22:

```sh
uv sync --locked
HELPDESK_TRACING_ENABLED=false uv run python -m helpdesk.evals --mode protocol
node --test frontend/tests/*.test.mjs
```

After configuring Anthropic and local Langfuse, run the paid completion eval:

```sh
uv run --env-file .env --env-file .env.langfuse python -m helpdesk.evaluation \
  --dataset evals/completion-datasets.json
```

### Guardrails and limits of the evidence

The input guardrail also uses Claude Sonnet 5.5, the same model as the main
agent. It runs as a separate, tool-free classification call before the agent
receives the message, with low effort and a one-token output limit. It returns
only `0` for Helpdesk or `1` for blocked requests.

Blocked input receives a fixed reply and adds no text, intent, response,
or placeholder to history or facts. Invalid or unavailable classification returns
a technical error. A later valid message remains usable.

Schema validation, backend authorization, approval checks, execution budgets,
and treating ticket content as untrusted remain independent code controls.

Labels and judge anchors were AI-assisted. Inspected cases are regression data,
not unseen evidence. A same-family judge can share model errors, and a finite
attack set cannot establish general injection resistance. Independent human
labels, external security testing, load tests, and multi-worker validation are
still missing. Most eval traffic uses in-process HTTP; container smoke tests
separately exercised actual services and provider calls.

### Try controlled failures

Only an operator with the mock service key can enable faults. Load `.env` into
your shell, then make the next two eligible reads return a rate limit:

```sh
set -a
. ./.env
set +a
curl -X PUT http://127.0.0.1:8001/admin/failures \
  -H "X-Mock-Service-Key: $MOCK_SERVICE_KEY" \
  -H "Content-Type: application/json" \
  -d '{"mode":"rate_limit","target":"read","remaining":2,"retry_after_seconds":0.2}'
```

Modes are `off`, `rate_limit`, `server_error`, `slow`, and `committed_timeout`.
Use `target` to select `read`, `write`, or `any`; `remaining` bounds the faults.
`slow` uses `delay_seconds`. `committed_timeout` saves an approved write and then
reports an unknown outcome; it does not affect staging. GET `/admin/failures`
shows settings; PUT `{"mode":"off"}` disables them. Restart resets the switch.
Authorization is checked before faults. Browser users and tools cannot set them.

## Observability

Each request has a correlation ID across guardrail, model, MCP, backend, and
approval activity. Traces include model settings, available token usage, tool
arguments/results, latency, retries, errors, approval decisions, and write outcomes.

| Langfuse environment | Traffic |
| --- | --- |
| `development` | Normal application requests, rooted at `helpdesk.request` |
| `protocol-tests` | Deterministic correctness checks |
| `sdk-experiment` | Real-model experiments and judge runs |

To investigate a bad run:

1. Find its request trace and check whether screening blocked, failed, or allowed it.
2. Inspect the model's actual calls and the tools available at that point.
3. Check MCP validation, backend permissions, and bounded retry attempts.
4. For a write, compare the saved proposal, later decision, and backend outcome.
   An unknown outcome must be reconciled with the same action ID.
5. Compare the validated answer with successful tool evidence. Treat a partial
   stream as incomplete even if some text reached the page.

Credentials are masked. Raw thinking, exception text, and blocked message text
are excluded. Parent spans do not duplicate generation usage. Unavailable physical
SDK retry counts and billed cost are not invented.

**PII detection and redaction are not implemented.** User and ticket content can
reach Anthropic, SQLite, and traces. Credential masking is not general PII protection.
History and trace cleanup also need an explicit retention policy.

## Key technical decisions and why

| Decision | Reason and tradeoff |
| --- | --- |
| SQLite instead of adding a database service | Simple local setup and atomic approval/result storage; concurrency remains limited |
| Separate MCP and mock HTTP services | Makes the agent boundary and user permissions testable; adds service calls |
| Local discovery and an overview before details | Makes current access visible before ticket work; adds a tool round trip |
| Approval enforced in code | A model suggestion cannot authorize a mutation; requires durable pause and recovery state |
| Buffered final-answer SSE | Prevents intermediate tool narration from leaking into chat; first visible text waits for generation to finish |
| Narrow OS fact extractor | Meets cross-session memory needs without storing arbitrary facts; only limited English statements are recognized |

The application uses one worker per service and serializes chat requests.
Those are local scope choices, not a claim of production scalability. Search
remains lexical. The input classifier reduces unwanted requests but is not a
complete security boundary. AWS remains a design-only exercise.

## Engineering standards

### AI solution architecture

The design applies selected principles from Anthropic's *Building effective
agents*, *Writing effective tools for agents*, and *Demystifying evals for AI
agents*. These are engineering recommendations, not a certification or a formal
compliance standard. The following is how we applied them to this assignment.

| Principle | Application in this project |
| --- | --- |
| Start with the simplest useful design | One Helpdesk agent, five domain tools, SQLite, and a thin browser page. No supervisor or multi-agent handoffs. |
| Separate predictable workflow from model judgment | Code handles screening routes, identity, approval, and persistence. The model chooses lookups, asks clarifying questions, and proposes changes. |
| Design the agent-tool interface carefully | Action-based names, explicit schemas, selection guidance, paginated results, and concise errors help the model choose and use tools. |
| Bound autonomous execution | Request deadlines and model/tool budgets stop runaway loops. Ticket mutations pause for explicit user approval. |
| Verify outcomes as well as responses | Code graders inspect tool arguments and database changes. A separate judge assesses answer quality; traces explain failures. |

We adapted these principles to the assignment. We chose a framework for durable
state and interruption, and checked its behavior through integration tests.
Independent human grading and repeated trials on unseen cases remain gaps in
our evaluation process.

### Service boundaries and contracts

The application follows practical microservice design principles at a small
scale: separation of responsibilities, explicit contracts, and ownership of data.
Chat owns conversation orchestration, MCP owns the model-facing tool interface,
and the mock owns ticket rules and mutations. Agent state and ticket data live
in separate databases. The agent cannot query the ticket database directly.

Pydantic request/response models and MCP schemas define the boundaries. Each
boundary validates untrusted input and returns explicit errors. Service
credentials authenticate MCP to the mock; separate user credentials determine
whose rights apply. The backend remains responsible for ticket authorization,
including when an approved operation is retried.

The services run in separate containers with health checks and environment-based
configuration. This is a small service-oriented application in one repository
with one shared image and release cycle. Independent releases, a service mesh,
a message broker, and distributed transactions would add complexity without
serving the local assignment.

### Function design and code organization

We use single-responsibility functions and modules where they clarify a real
boundary. Agent orchestration, guardrails, MCP transport, ticket rules, storage,
and HTTP handling have separate locations. We avoid introducing abstraction
layers solely for possible future features.

Python names use descriptive `snake_case` for functions and variables and
`PascalCase` for classes. Docstrings follow NumPy style: a concise summary, with
`Parameters`, `Returns`, `Raises`, or `Notes` when useful. Comments explain
constraints and reasons. Single blank lines separate logical steps, and long
calls are wrapped for readability. Project text is in English, and prompts live
in dedicated modules instead of being mixed into orchestration logic.

Type annotations and boundary models make contracts easier to inspect, although
annotations are not yet complete or enforced by a static type-checking gate.
SQL values use bound parameters. Blocking application-storage calls are moved
off the asynchronous event loop so they cannot prevent checkpoint commits.

Application code lives in `backend/src/helpdesk/`, tests in `backend/tests/`,
browser code in `frontend/`, and schemas/fixtures in `data/`. Runtime databases
are ignored under `data/runtime/`. One `pyproject.toml` and `uv.lock` define the
Python environment.

### Reliability, transactions, and idempotency

External calls have explicit timeouts, bounded retries, and an overall request
deadline. Transient failures use backoff and bounded `Retry-After`; validation
and permission failures do not become retry loops. The model gets one chance
to correct invalid tool arguments, separately from transport retries.

Approval is a persisted state transition bound to a user, session, action, and
exact arguments. A prompt instruction cannot replace that check. The backend
commits the ticket mutation and its saved result in the same transaction under
a durable action ID. Replaying that approved action returns the stored result
instead of creating another ticket or comment.

This provides at-most-once mutation per approved action. It does not promise
exactly-once network delivery or collapse two separately approved actions.
A timeout can mean the write committed but its response was lost. Recovery
keeps the outcome unknown until the same action ID resolves it, and does not
start a new write to guess what happened.

Checkpoints and pending approvals survive restarts. Tests exercise decline,
duplicate approval, interruption during staging, lost write responses, and
SQLite writer contention. Coordination is validated for the local single-worker
setup; horizontal scaling needs different storage and concurrency verification.

### Security and observability

Least privilege applies at both service and user boundaries. Verified
credentials establish identity; user messages, remembered facts, and trace
metadata never grant access. Authorization runs before pagination and again
before mutations. Ticket content is untrusted, and approval endpoints are not
available to the model.

Secrets stay in environment configuration and are masked in traces. Containers
run as a non-root user and publish loopback ports. The browser keeps credentials
in page memory. Request correlation connects model, tool, backend, and approval
activity, while operational errors remain distinct from expected refusals.
These controls do not replace PII handling, production login, or a retention
policy, which are still incomplete.

### Verification and reproducible delivery

GitHub Actions runs Python/protocol checks, JavaScript tests, and a Docker build
on pull requests, pushes to `main`, and manual runs. It uses locked dependencies,
pinned action commits, read-only repository permissions, and no provider secrets.
Logs and reports remain as seven-day artifacts. Paid evaluations run separately;
branch protection needs separate configuration. Vendored browser code retains
its license.

Review fixes cover revoked ticket data returning through history, intermediate
SSE narration, a crash after proposal staging, and lost write-reconciliation
responses. A later CI failure exposed synchronous SQLite calls blocking an
asynchronous checkpoint commit. Those calls now run outside the event loop,
with a regression for actual writer contention. Original failed reports remain.

There is no configured lint or static type-checking gate, automatic history
cleanup, production OAuth, deployment validation, or multi-worker guarantee.
The Docker CI job builds the image; it does not prove successful runtime behavior.

## How AI tools were used

An AI coding assistant helped research APIs, compare approaches, draft code and
prompts, build fixtures and evals, investigate failures, and prepare documentation
and diagrams. Separate AI research tasks explored ticket search, project layout,
and provider logging. An Astra code review identified four defects that were
corrected with regression tests.

The user directed the scenario, stack, model choices, tool behavior, approval
flow, guardrail policy, memory scope, and scope reductions. AI-generated output
was revised through those discussions and checked through the recorded tests
and experiments. AI-generated eval labels and AI review are not independent
human verification.

## What would change with more time

- Run the paid suites on the final source revision and add fresh, independently
  reviewed cases that were not used while tuning prompts.
- Add PII handling before model calls, storage, tracing, and memory extraction,
  plus explicit history/trace retention and deletion rules.
- Replace demo tokens with verified login and delegated access. Keep ticket
  authorization and approval enforcement in backend code.
- Test PostgreSQL-backed checkpoints and coordination before running multiple workers.
- Improve search when a larger dataset justifies synonyms or hybrid retrieval.
- Add lint/type checks, load tests, and broader failure and security testing.
- Validate the proposed AWS design in an actual account, including recovery,
  identity propagation, networking, quotas, and cost.

## Candidate perspective

My priority was to build a small Helpdesk assistant whose actions I could inspect
and explain. I wanted to participate in the design decisions, so we worked
through the stack, tool contracts, permissions, approval flow, memory, and
observability step by step. I repeatedly narrowed the scope to what the
assignment needed rather than building a general platform.

I wanted the model to orchestrate the task: understand the request, clarify
missing details, and choose tools. At the same time, I wanted access through
the agent to match the user's existing rights. That led to an explicit split
between model judgment and application control over identity and writes.
A proposed change only becomes an authorized operation after the user's next
message approves its exact contents.

I chose Python, a framework-backed ReAct agent, SQLite, and a simple frontend
to keep the implementation manageable. I would keep the explicit approval
boundary and durable idempotency even in a larger version. I would reconsider
the single-worker storage design and lexical search when actual concurrency
and retrieval requirements justified it.

I also wanted failures to be visible. Restricted tickets, controllable backend
faults, adversarial inputs, and traces made it possible to examine behavior
beyond a successful demo. Passing a finite test set is useful evidence, but I
would not treat it as proof that the agent is secure or production-ready. My
next validation priority would be a fresh model run on the final revision,
with independently reviewed cases and repeated trials.

AI assistance was substantial and is described above. This perspective was
also drafted with AI from the decisions I made during development. The recorded
tests support specific implementation claims; they do not replace my ability
to explain the code, its tradeoffs, and its remaining limitations.
