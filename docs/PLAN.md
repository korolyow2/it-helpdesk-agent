# IT Helpdesk implementation plan

## Goal and scope

Build the smallest complete local solution that meets the assignment requirements.
The agent searches tickets, reads their status, and proposes either creating a
ticket or adding a comment. Each approved proposal performs one change.
Keep the frontend simple. Do not add optional features or general-purpose
platform infrastructure.

## First end-to-end scenario

1. The user says: "My VPN is not working."
2. The agent searches for similar tickets using a paginated MCP tool.
3. The agent reads a relevant ticket and its status through MCP.
4. The agent proposes a specific comment on a relevant existing ticket, or a new
   ticket if there is no suitable match. It persists the proposal and stops.
5. The user approves or declines in a subsequent message.
6. Approval executes the exact proposed change once through MCP. Decline makes
   no change. The agent reports the outcome.

## Acceptance criteria

- No write occurs before explicit approval, including when the model requests it.
- Declining a proposal leaves tickets and comments unchanged.
- A service restart preserves the pending proposal and allows approval afterward.
- Repeated approval and retries cannot create duplicate tickets or comments.
- A transient failure triggers bounded retries. Persistent failure or a slow
  response ends within a deadline and produces an honest user-facing result.
- A committed write whose response is lost can be retried without duplication.
- Session history is retained, and at least one useful user fact is available
  across sessions. Another user cannot access that history or memory.
- The agent accesses ticket data only through MCP. The MCP server authenticates
  to the mock backend with a key or token.
- Each request has a trace covering model calls, tool arguments and results,
  latency, available token usage, and errors, with secrets redacted.
- The web page shows tool calls and supports approval and decline.
- Evals run with one command and cover tool selection, approval, out-of-scope
  refusal, and failures enabled.

## Implementation order

1. Agree on the language, minimal stack, and initial model provider. Define the
   tool contracts and durable proposal states before implementing them.
2. Build a small seeded mock and its authenticated API, then MCP search/read/write
   tools. Enforce write idempotency where the mock commits the change.
3. Implement the agent HTTP endpoint and the first end-to-end scenario, including
   persistent approval, session history, basic user memory, and request traces.
4. Verify approval recovery and duplicate prevention. Add controlled mock failures,
   bounded retries, and deadlines; verify uncertain write outcomes.
5. Add a thin web page, finish the required eval cases, and record actual results.
6. Complete containerized startup, README, the AgentCore design document, and
   demonstration evidence. Review the submission against the source assignment.

## Selected technologies

- Language: Python.
- HTTP backend: FastAPI with Pydantic response schemas.
- Dependency management: uv with `uv.lock`; application startup uses Uvicorn.
- Agent orchestration: `langchain.agents.create_agent`, backed by LangGraph.
  Use its ready-made ReAct loop; no manually assembled graph or supervisor.
- Initial model: Claude Sonnet 5 (`claude-sonnet-5`) directly through the Anthropic API.
- Frontend: plain HTML and JavaScript.
- Storage: SQLite for mock tickets and persistent agent state.
- MCP server: standalone FastMCP (Python), using Streamable HTTP for runtime
  connections and its stateless HTTP stack in integration tests. FastMCP 4.1.1
  and the corresponding SDK versions are pinned in `uv.lock`.
- Agent MCP integration: LangChain MCPAdapter (`langchain[mcp]`), which uses
  FastMCP. This integration is currently beta; pin compatible package versions
  and verify discovery, tool results, errors, and approval/resume end to end.
- Observability: locally self-hosted Langfuse using an adapted official Docker
  Compose setup. Explicit SDK observations instrument the agent middleware,
  model context, MCP/mock requests, retries, and approval transitions without
  adding a duplicate LangChain callback tree.
- Evaluation: a local Python evaluation command with code-based checks and
  results linked to Langfuse traces/experiments. Use pytest for deterministic
  correctness tests. Add model-judged checks only where code cannot assess the
  desired behavior; they must not replace approval or idempotency assertions.
- Keep the model provider configurable as required by the assignment.

Langfuse runs its own supporting services and storage; the application's SQLite
choice does not configure Langfuse storage. No LangSmith service is required. Its SDK is a transitive LangChain dependency;
no LangSmith tracing is configured.
The mock, MCP, and read-only agent dependencies are installed and locally verified.
Langfuse integration is configured and local ingestion is verified. Real-model
behavior now has an initial Sonnet 5 trial with an explicitly retained failure.

References:
- https://docs.langchain.com/oss/python/langchain/mcp
- https://github.com/PrefectHQ/fastmcp
- https://langfuse.com/integrations/frameworks/langchain
- https://langfuse.com/self-hosting/deployment/docker-compose

## Ticket storage decision

Store comments as a JSON array in the `tickets.comments` field, initially `[]`.
Do not create a separate comments table for this assignment. Each comment carries
an id, author id, body, and UTC creation timestamp. Appending a comment must be
atomic and retries of the same operation must not append duplicates.

Add a visibility field with `internal` and `restricted` values. Seed six internal
tickets (TKT-001 through TKT-006) and two restricted tickets (TKT-007 and
TKT-008). Employees can access internal tickets; Support can access all tickets,
including restricted ones. The fixture is defined in `data/seed.json`; the schema
is in `data/schema.sql`. Users contain identity and role
only, without credentials or pre-populated conversational memory. Synthetic
markers in restricted descriptions make accidental disclosure detectable.

### Database initialization

Run `uv run python -m helpdesk.mock.init_db` from the project root to create
`data/runtime/helpdesk.sqlite3`. An optional `--database PATH` selects a different output.
The loader uses Python's standard library and commits schema creation and fixture
insertion in one transaction. It refuses to initialize an existing schema rather
than overwriting data. Generated database files are ignored by Git.

The schema enforces unique non-null IDs, requester references, role/status/visibility
values, and valid JSON arrays for comments. Every future database connection must
enable `PRAGMA foreign_keys = ON`. Comment item validation and access control belong
to the upcoming mock service; this schema does not enforce user permissions.

Manual checks passed for fixture counts, integrity, eight invalid-value cases,
preservation of an edited database on rerun, and complete rollback for an invalid
fixture. These are local initialization checks, not application eval results.

## Authentication and authorization requirements

- Authenticate users before accessing agent sessions or ticket operations.
  Derive identity from verified credentials, not from model arguments. If the
  HTTP request includes a user id, require it to match the authenticated user.
- Apply ticket permissions to search, read, and write operations. Filter search
  results before pagination and do not disclose inaccessible ticket content.
- Check permission to access Helpdesk itself before checking individual tickets.
  A user without Helpdesk access receives no Helpdesk tools. The MCP server must
  independently reject direct tool calls from that user. Agent service credentials
  must never elevate the user's permissions.
- The mock enforces permissions using trusted caller identity. The MCP server's
  service credential authenticates the service; it does not grant every user
  unrestricted ticket access. Recheck permissions when executing an approved write.
- Keep session history, pending approvals, and user memory private even when
  ticket access is shared. A user's stated team in memory never grants permissions.
- The fixture target is four users and eight tickets, adding a user without
  Helpdesk access to the original three-user set. Include allowed
  and forbidden operations, unauthenticated requests, identity spoofing, and
  cross-user session/approval access in verification.

### Agreed role matrix

| User | Role | Ticket permissions |
| --- | --- | --- |
| user-001 | Employee | Create internal tickets as themselves; read and comment on internal tickets |
| user-002 | Employee | Create internal tickets as themselves; read and comment on internal tickets |
| user-003 | Support | Read and comment on all tickets; create tickets as themselves |
| user-004 | No Helpdesk access | No ticket search, read, creation, or commenting |

Support access to tickets does not grant access to other users' conversations,
memory, or pending approvals. Ticket ownership comes from authenticated identity.
The mock validates a service key plus a separate mock-specific user token.
MCP validates separate local user tokens and checks live Helpdesk membership.
Agent-facing authentication is not implemented yet.

Most tickets are internal and accessible regardless of requester; this replaces
the earlier requester-only access proposal. Employees cannot select `restricted` visibility
when creating tickets. Include similar issue descriptions in both visibility
groups to test that relevance never overrides authorization.

Verify that restricted tickets do not appear in employee search results, counts,
or pagination; direct reads and comment attempts must fail without disclosure or
mutation. Include an end-to-end demonstration of a denied operation and its trace,
without logging restricted ticket content in the employee's trace.

## Pending decisions

- Anthropic credential availability and real-model validation.
- Compatible dependency versions for the upcoming agent integration.
- Retrieval quality on real-model evaluation cases beyond the seeded examples.
- Evaluation cases/metrics and application packaging tools.
- Final agent-facing authentication and per-request MCP client wiring.
- The cross-session user fact and its retention policy.

Select these for this assignment; do not implement speculative extensibility.

## Current implementation checkpoint

`backend/src/helpdesk/mcp/server.py` exposes search_tickets, get_ticket,
create_ticket, and add_comment. It accesses the mock only through HTTP, uses
verified user subjects, and never forwards MCP credentials to the mock. A live
membership check prevents users without Helpdesk access from discovering tools.

The mock API and `mock/store.py` implement exact-ID and role-filtered keyword search and
durable approval-gated writes. Search ranks literal title/description matches
and filters permissions before counts and pagination. Write tools first return
a persisted proposal. An explicit decision is recorded through an application-only
HTTP endpoint, never an MCP tool. Execution checks the same user, session, operation,
exact arguments, and current permissions. Mutation and completed result commit
together; retries reuse the approval ID as the durable idempotency key.

`data/approval_schema.sql` adds approval storage. Initialization includes it;
`python -m helpdesk.mock.init_db --upgrade` adds it to an existing database without
reseeding. The existing local database was upgraded with users and tickets preserved.

At the argument-correction checkpoint, 205 correctness checks passed, including authenticated stateless MCP HTTP tests,
the complete initial access matrix, filtered pagination, decline, ownership and
session isolation, restart recovery, parallel retries, lost responses after commit,
transaction rollback, bounded failures, and non-destructive schema upgrades.
These use temporary databases and in-process HTTP transports, without a model.

The agent's next-message approval flow, user memory, UI, interactive mock failure
switch, and real-model quality results remain incomplete. Local Langfuse tracing
has since been implemented. Tests currently
simulate failures through controlled transports. See `docs/MCP.md` for contracts
and limitations.

See README for initialization, configuration, startup, and test commands.

## Repository organization

Use a single root Python project with an editable `helpdesk` package under
`backend/src/`, tests under `backend/tests/`, and plain browser assets under
`frontend/`. Keep reproducible source data under `data/` and local mutable
databases under ignored `data/runtime/`. This is a checkout-based application;
initialization reads the repository's schema and seed files.

Supporting documents live under `docs/`, including this plan and the unchanged
source assignment under `docs/assignment/`. Keep the main README and future
AGENTCORE.md in the repository root to match the submission requirements.


## Ticket lookup and error recovery checkpoint

A known ticket ID can go to `get_ticket` after the mandatory overview. Search also extracts exact IDs
from user text, normalizes case, and preserves access filtering and pagination.
When IDs are present, it does not fall back to unrelated keyword matches.

The `mock/` and `mcp/` component folders separate the API/store from tool exposure,
authentication, and the retrying HTTP client. Shared safe error definitions live
in `helpdesk/errors.py`. Normal error results contain is_error, code, message, and is_retryable.
Write outcomes and rate-limit waits are included only when relevant; diagnostic
metadata stays internal; see `docs/ERRORS.md` for the full policy.
Membership and data calls share a deadline. Transient failures receive bounded
retries; permanent and authorization failures stop. Uncertain writes retain the
same approval identity, including when a later attempt is denied.

Interactive recovery actions remain part of the upcoming agent/application work.
No real-model recovery behavior has been evaluated yet.


## Read-only create_agent checkpoint

`agent/graph.py` creates the ready-made ReAct agent with a model/tool call budget
and middleware that stops tool execution after an error. `agent/prompts.py`
defines the English Helpdesk scope. `agent/service.py` supplies per-user MCP HTTP
credentials, fresh discovery, isolated SQLite history, a total deadline, and a
trusted local CLI. The original two read tools are now joined by `list_tickets`.
Historical ticket access
is rechecked before replaying stored tool results to a model. Interrupted reads
are marked explicitly before the next user message.

The MCP integration uses `MCPAdapter` from `langchain[mcp]`, compatible with our
FastMCP/MCP SDK. The old standalone adapter failed an import check and was removed.
The integration is beta, so its locked versions and HTTP integration tests matter.

Thirteen new agent tests use scripted model decisions and the real factory,
adapter, MCP HTTP stack, mock, and SQLite. They verify transport and permissions,
not Sonnet decision quality. Sonnet 5 is now selected; no paid model requests were made. See README for the local command and limitations.

Next-message approval, cross-session facts, the chat UI, and real-model quality
results remain incomplete. See the later tracing/evaluation checkpoint.


## Sonnet 5 and answer streaming checkpoint

The user selected Sonnet 5 and authorized parameter selection. Defaults are
adaptive thinking, medium effort, 1024 max tokens, no sampling overrides, a
30-second provider timeout, up to two SDK retries, and a 60-second run deadline. Thinking consumes part
of max_tokens. These are initial settings, not results from real-model evals.

The service now uses create_agent.astream with v2 messages/state events. The
public HTTP endpoint POST /chat/stream emits only answer text, completion, and
terminal errors. It excludes thinking and all tool metadata/results. POST /chat
collects the same run for nonstream consumers. API bearer authentication binds
the body user ID to the configured credential before starting the response.
The API service/checkpointer is managed by application lifespan. Model and MCP
streams are closed when the consumer disconnects.

Streaming is the one selected optional assignment feature. Deterministic tests
prove progressive delivery over HTTP, omission of private thinking and tool data,
minimal terminal errors after partial text, identity binding, safe input errors,
truncation handling, and supported Sonnet request payloads. The browser chat is
still a future stage; no real Anthropic call has been made.


## Context preparation pre-hook checkpoint

The first agreed pre-hook prepares a compact model view on every model call.
It uses wrap_model_call/request.override rather than a state-mutating before_model
hook, keeping full SQLite history unchanged. An approximate 8000-token input
budget includes instructions and tool schemas. Old exchanges are removed only
as complete user turns. The current question, all current tool-call/result pairs,
and provider thinking signatures are retained. Oversized current context fails
explicitly. Tool JSON is compacted without changing values, and private artifacts
are omitted from the ephemeral message objects; the SDK already excludes those
artifacts from Anthropic model content. No additional model call is needed.

Six focused checks cover valid protocol groups, complete history retention,
provider signatures, preservation of values, mandatory context accounting, and
oversized-current-turn handling. User fact extraction, memory retention, and
approval integration are still separate steps.

## Tool argument correction checkpoint

The tool-call pre-hook validates generated arguments against the discovered MCP
JSON schema before execution. Server and mock validation remain authoritative.
After `invalid_arguments`, the agent offers one corrected call of the same tool
with changed arguments, within the existing four-model/six-tool budgets. An
unchanged or still-invalid correction ends tool use for that user turn. Other
tool errors do not grant a correction or restart network retries. A successful
correction allows the normal read workflow to continue. The next user message
gets a new correction budget; error responses retain the minimal public schema.

Fifteen new deterministic integration checks cover missing fields, types, bounds,
unknown keys, malformed IDs, server-side validation, unchanged arguments,
exhausted budgets, tool switching, recovery, and service failures. The complete
suite passes: 205 tests, with the existing LangChain MCP beta warning. Source
compilation also passes. No real-model requests were made. The user confirmed
the current 1024-token model output limit; documentation and its payload check
now match that setting.

## Mandatory overview, tracing, and eval checkpoint — 2026-10-20

`list_tickets` now returns paginated permission-filtered IDs, titles, and statuses
through MCP and the mock API. Every user turn begins with an application-issued
first page before model decisions or other ticket reads. That call consumes one
of six tool calls; model calls remain limited to four. Context and tool
descriptions explain incomplete pages and candidate verification. This catalog
strategy is deliberately limited to the assignment's small fixture.

Local Langfuse v4.64.0 runs through `compose.langfuse.yaml`; Python SDK v5.0.3 is
locked. Generated credentials are ignored and owner-readable. Actual server
ingestion and locally computed scores were verified through the current
Observations/Scores APIs. Spans cover requests, effective model context,
available usage, tools, permission boundaries, HTTP attempts/backoff, approval
decisions, committed writes, and idempotent replay. Secret masking, user-scoped
session grouping, and known privacy/retry limits are documented in
`docs/OBSERVABILITY.md`. No paid provider calls were made.

`python -m helpdesk.evals` runs a 53-case bank: 20 protocol groups, 24 real-model
tasks, and nine explicit pending capabilities. The measured run passes all 231
pytest checks and all 20 mapped protocol groups. Real-model cases are blocked by
missing Anthropic credentials, not passed using prepared decisions. Pending
chat approval, cross-session facts, browser flows, and a runtime failure switch
remain visible. Exit code 2 signals these blockers. Prepared model decisions
also test the eval harness itself and do not demonstrate Sonnet behavior.

`evals/results/latest-all.json` records the actual results and source/dataset/
prompt/dependency hashes. `docs/EVALUATION.md` explains graders, failure injection,
human-review limitations, and separate real-model trials. No general platform,
LLM judge, cloud deployment, or deferred PII detector was added.

## Real-provider baseline — 2026-10-20

The user supplied an Anthropic key for the pending real eval. The key was moved
from the example file to owner-only ignored `.env`, and the example was restored
to a placeholder. API preflight required an explicit workspace header. Read-only
discovery returned exactly one workspace, Default; `ANTHROPIC_WORKSPACE_ID` now
selects it through ChatAnthropic's default headers, outside model input.

The real Sonnet 5 smoke case passed. A subsequent full trial passed 232 protocol
tests and 23 of 24 real-model tasks. M24 correctly read the requested ticket but
answered in Russian despite the English-only system policy. The original failure
is retained; full-run exit status is 1. Nine pending capabilities remain blocked.

The full model suite used 45 logical model calls and reported 138,315 input and
5,894 output tokens. These totals exclude the separate two-call smoke trial.
Actual SDK HTTP retries and monetary cost are not inferred. All 44 case scores
(20 protocol groups plus 24 real tasks) were read back from local Langfuse with
the matching eval run ID. The key was checked absent from project artifacts.

AI-assisted transcript review noted issues beyond binary graders: unnecessary
access claims, a retry offer after exhaustion, ambiguous read/create wording,
and verbose replies. These are review notes, not calibrated human evaluations.
See `evals/results/latest.md` and `real-model-summary.json` before drawing broader
quality or reliability conclusions from a single trial.


## Model-first orchestration and native experiments — 2026-10-20

This checkpoint supersedes the earlier automatic-overview design. The model now
chooses every ticket call; middleware exposes only the overview until it succeeds,
then enables the remaining read tools. Out-of-scope turns need no ticket reads.
Historical ticket evidence is gated by fresh overview access without hidden read
RPCs. Authentication and membership discovery remain mandatory before model use.
The budgets are six model calls and six tool calls within the existing deadline,
allowing overview, paginated search, candidate read, and a final answer.

A principle-based prompt replaces the procedural script. A fixed English product
contract was strengthened after the preserved language regression recurred in a
smoke trial. Six synthetic judge anchors agreed with an independent judge call;
this is not independent human calibration. Native versioned datasets and local
SDK experiments now measure code correctness, tool arguments, answer quality,
and adversarial scope/instruction adherence. Earlier reports remain historical
baselines; current evidence is linked from README and docs/EVALUATION.md.


The final evaluation iteration uses dataset v3 and a principle-based prompt:
successful backend reads authorize returned fields, retrieved directives cannot
change permissions, malicious instructions should not be reproduced, and explicit
search filters must be honored. Prompt-only language and injection-echo failures
remain in repeated trials; no guardrail was added to hide them. A typed empty
provider refusal was added and verified separately.

The evaluation grader was also corrected: effective pagination defaults are
accepted, pure credential-disclosure refusal requires no ticket tools, and the
LLM rubric recognizes authorized Support reads. Nine synthetic rubric anchors
agree with expected labels. Native cached rejudging retains actual agent outputs,
original grades, and source run IDs; it is not another agent trial. Final measured
results and known gaps are linked from evals/results/latest.md.


## LangGraph input routing and browser interface — 2026-10-21

Implemented an outer stateless LangGraph workflow with conditional routes from
the input classifier to the persistent create_agent graph or a fixed reply.
Rejected input, intent and static replies do not create conversation checkpoints.
The child graph retains its verified user/session root namespace and SQLite
history; restart, interrupted reads, deadlines and stream cleanup tests pass.

Implemented the HTML/JavaScript interface using locally vendored Deep Chat
2.5.1. It sends only the latest user message, streams user-facing answer text,
marks incomplete replies honestly and displays tool names/statuses from a
separate authenticated completed-request lookup. UI credentials stay in page
memory; no provider key is exposed. Browser approval remains pending with the
conversational write integration. Python suite: 276 passed. JavaScript parser:
8 passed. Browser smoke checks use synthetic responses, not paid models.


## Guardrail observability and prompt alignment — 2026-10-21

Guardrail generations now export application-generated code/intent/decision
records, while the classifier still emits one plain digit. Invalid output,
provider refusal/failure and cancellation have distinct safe diagnostic outcomes.
Request metadata mirrors allow/block/error, with blocked requests labeled
blocked rather than completed. The main prompt reflects upstream routing,
retains authorization boundaries, and shares the fixed fallback reply with the
workflow. Rejected text and decisions remain outside conversation history.

The Python suite passed 282 tests. Local Langfuse read-back verified all four
synthetic cases (0, 1, 2 and invalid output) under one root with 11 observations,
no missing parents and no rejected text. This used no provider API calls and
is not a semantic quality evaluation. Evidence:
`docs/evidence/guardrail-observability.json`.


## Binary guardrail and full v5 evaluation — 2026-10-21

The guardrail now accepts only 0 (Helpdesk) or 1 (unrelated/unsafe); code 2 is
invalid. Dataset v4 and its case bank are archived. Version 5 freezes 54 cases
with per-turn binary labels and adds two blocked-then-valid recovery scenarios.
Expected changes are recorded before trials in `evals/archive/v5-contract-changes.json`.
No post-trial prompt or expectation tuning was applied during the full run.

Python tests: 282 passed. Preliminary Q05 real smoke: 1/1 passed. Full Sonnet 5
run: 50/54 joint passes, 55/58 correct guardrail decisions, one false block,
one false allow and one unavailable decision. M09 omitted a required direct
read; A13 blocked a mixed benign request; A15 passed an unsafe instruction and
quoted the injection canary; A04 received provider refusals from screening and
judging. All are retained in the raw report. G01/G02 recovery both passed.

Local Langfuse verified 54 experiment items and scores, 58 guardrail generations,
134 agent generations, 54 judge generations, one root per item and no missing
parents. Main-model execution preceded MCP in all 32 retrieval turns. See
`evals/results/latest.md` and the linked API verification for metrics and limits.


## Follow-up corrections and final read-only evaluation — 2026-10-21

The frozen v5 dataset and criteria are unchanged. Tool discovery now distinguishes
the required first overview from capabilities unlocked next; every model call
receives the effective tool names. Direct ticket lookup no longer depends on an
entry in the overview. The guardrail distinguishes authority overrides, legitimate
mixed requests, opaque record references and technical pretexts. It still emits
one plain binary digit with disabled thinking and one output token.

Intermediate full runs passed 52/54 twice. They exposed M13 false acceptance and
A08 false acceptance; several smaller revisions also incorrectly blocked A13.
All original reports and both intermediate full-run API proofs remain intact.
The final classifier-only catalog probe classified 57/58 inputs correctly,
with only A04 unavailable; it reused prior real replies for follow-up context.

The final new end-to-end run `fa35488522f4472a86a40360fe02a58b` passed 53/54 jointly.
All 57 available guardrail decisions were correct; the missing decision remains
in the 58-turn denominator. A04 is a provider refusal, not a successful block,
and its judge result is also unavailable. No fallback model is enabled without
agreement on changing the auxiliary model. Judge rubric v3 uses the same five
criteria with clearer evidence rules, medium effort and a 2048-token offline
budget; the main agent remains at 1024. Calibration agreed on 9/9 anchors after
an earlier 8/9 repeat, all retained. Final Python checks: 282 passed; frontend
stream parser: 8 passed. See `evals/results/latest.md` for trace verification,
latency, preserved failures and remaining assignment gaps.


## Auxiliary fallback and Opus 5.5 judge — 2026-10-21

The user authorized reserve calls for input screening and judging, and explicitly
selected `claude-opus-5-5` as the primary judge. The agent and primary classifier
remain `claude-sonnet-5`. The Models API returned no permitted server-side
fallbacks for Sonnet 5; one client-side `claude-opus-4-8` invocation is therefore
allowed only after a provider refusal. Invalid output, transport failures,
timeouts and reserve refusal do not start another model chain. Strict decision
and judgment validation remain in place.

Logical `guardrail.input` and `judge.answer` spans contain individual generation
children (`guardrail.attempt`, `judge.attempt`) with actual models and reported
usage. Parent spans do not duplicate usage. Seven new tests cover reserve bounds,
strict validation, history exclusion, model configuration and usage accounting;
the Python suite passed 289 tests. Opus 5.5 calibration agreed on 9/9 anchors.

Preliminary A04 repeats passed 2/2 jointly. The first complete run with the new
judge passed 54/54 code and 53/54 jointly: Q02 inferred a permission limitation
from authorized ticket content. The main prompt now separates absent findings
from denied access and preserves canonical statuses. A first repeat passed 1/2
because a status was paraphrased; the corrected Q02 repeat passed 2/2. Negative
judgments and raw reports remain intact.

The final new full run `52f36aedcf6644adbc51e432006d2fac` passed 54/54 jointly,
with all 58 binary routing decisions correct and all 20 blocks leaving history
unchanged. The frozen dataset and code criteria did not change. One final trial
per inspected synthetic case does not establish production reliability or
complete the still-pending conversational writes and memory features. Detailed
results and Langfuse verification are linked from `evals/results/latest.md`.

## Sonnet 5.5 migration and assignment review — 2026-10-21

The user selected Sonnet 5.5 for the agent and primary input classifier.
Defaults and local/example configuration now use `claude-sonnet-5-5`; the judge
remains Opus 5.5. Adaptive thinking, medium effort, 1024 output tokens and
transport bounds remain unchanged for the agent. The classifier replaces the
unsupported disabled mode with `between_tools`, low effort and one output token.
The tool-free Opus 4.8 refusal reserve normalizes that mode back to disabled.
This follows the official Sonnet 5.5 migration guide linked in the status audit.

The guardrail's case-shaped few-shot examples have been removed in favor of
ordered general rules. The runtime agent, discovery and MCP instructions contain
no fixture IDs or expected eval answers. This is a prompt audit, not proof of
generalization: all frozen v5 cases have already been inspected. Preserve their
labels and historical results. The new configuration passed 290 correctness
tests, eight frontend stream tests and a separate two-case real-model smoke.
The full migration run is recorded in the current measured report.

The source assignment was read in full and compared with the application,
backend/MCP contracts, browser code, eval catalog and delivery files. The
requirement matrix and recommended next discussion are in
`docs/ASSIGNMENT_STATUS.md`. Conversational proposal/approval/write, cross-session
facts, browser approval, the runtime failure switch, application containers,
AgentCore design and final end-to-end evidence remain incomplete. Those features
were not implemented as part of this model and prompt review.

The full Sonnet 5.5 migration run passed 53/54 jointly. G02 failed with an
AnthropicConnectionError during the legitimate follow-up's input check; two
unchanged repeats passed. Labels and prompt remained frozen. Langfuse API
read-back confirmed all 54 native items and joint scores; export warnings mean
complete span coverage is not claimed. The original failure remains in the
measured report and does not become 54/54 through selective reruns.

## Local Git repository — 2026-10-21

Initialized the existing workspace as a Git repository with `main` as the initial
branch. No commit or push was made. Ignore all local `.env.*` configurations
except the credential-free `.env.example`, as well as runtime databases,
virtual environments and Python caches. A scan of the 230 candidate files found
no configured secret values; the candidate set was approximately 24.7 MB with no
individual file over 50 MiB. The user selected local Git only. Do not create a
GitHub repository or configure a remote unless separately requested.

## Initial GitHub publication — 2026-10-21

The user created `https://github.com/korolyow2/it-helpdesk-agent.git` and explicitly
requested pushing the project there. This supersedes the earlier local-only
choice and authorizes the initial commit and push to `main`. Remote inspection
found no existing refs. All 230 candidate files passed a configured-secret and
private-key scan; local credentials, runtime databases and caches remain ignored.
Publish the current implementation and its honest evaluation/assignment gaps;
publication does not mean that the unfinished assignment requirements are met.
