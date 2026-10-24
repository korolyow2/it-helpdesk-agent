# Assignment readiness review

Reviewed on 2026-10-22 against the supplied
`AgentCore_Platform_Engineer_-_Assignment (1).docx`.

The main application requirements are implemented and pass the current
correctness checks. The submission still needs demonstration screenshots or a
recording, a final review of the candidate's written conclusions, and publication
of the remaining intended changes. Real-model results describe an earlier
revision and should be refreshed before claiming final model quality.

## Verification performed

This review checked the source assignment, application boundaries, approval and
storage logic, memory, frontend, failure controls, evaluation runners, tracing,
containers, CI configuration, and delivery documents.

| Check | Observed result |
| --- | --- |
| Full Python suite through the protocol runner | 338 passed; two pytest plugin-rewrite warnings |
| Mapped protocol cases | 33 passed; no failed cases or pending capabilities |
| JavaScript tests | 11 passed |
| Existing application containers | Chat, MCP, and mock running and healthy |
| Last completion model experiment | 17/17 code, judge, and joint passes; 30/30 guardrail decisions correct |
| Completion experiment source match | Does not match the current application hash |
| Tracked demonstration screenshots or recordings | None; the PNG in `diagrams/` is an architecture diagram |

The Python run started at 2026-10-22 18:57:35 UTC. It used tracing-disabled local
fixtures and synthetic model responses. No paid model calls were made during
this review. The existing protocol report was preserved after the fresh run.
The fresh runner ended with:

```text
338 passed, 2 warnings in 34.88s
case_counts: {"passed": 33}
pending_features: 0
```

The review checked existing container health; it did not rebuild from a clean
checkout, repeat the browser workflow, or test deployment on AWS. Passing these
checks does not prove that every possible failure or attack is handled.

## Requirement assessment

| Assignment requirement | Assessment | Implementation or evidence |
| --- | --- | --- |
| IT Helpdesk workflow | Implemented | Search, read status, propose ticket creation or a comment, then execute an approved change |
| HTTP agent endpoint | Implemented | Authenticated `/chat` and `/chat/stream` accept user, session, and message |
| Model provider configurable | Implemented with a stated limit | `MODEL_PROVIDER` and `MODEL_NAME` select the integration; only Anthropic is installed and evaluated |
| Custom MCP server | Implemented | FastMCP discovery and five tools, including paginated search, detail reads, and proposed writes |
| MCP authenticates to the mock | Implemented | Service key plus a separate credential for the verified user |
| Agent ticket access through MCP | Implemented | Ticket tools use MCP; the application-only approval path records decisions without giving the model approval authority |
| Stop before writing | Implemented | Proposal staging followed by a native LangGraph interrupt; application code verifies a later explicit decision |
| Approval survives restart | Implemented and tested | Persistent checkpoints, proposal bindings, backend records, and staging-crash recovery |
| Session and cross-session memory | Implemented and tested | User/session checkpoints and one expiring OS fact isolated by user |
| Controllable mock failures | Implemented and tested | Operator-controlled rate limits, server errors, slow responses, and committed-write timeouts |
| Bounded retries and timeouts | Implemented and tested | Backend and provider limits, backoff, request deadline, and explicit failure outcomes |
| Prevent duplicate writes | Implemented and tested | Durable action ID; mutation and reusable result committed atomically; repeated approval and uncertain outcomes reuse that ID |
| One-command evaluations | Implemented | Separate protocol and model runners; cases cover selection, approval, refusal, failures, and memory |
| Request traces | Implemented; export tested previously | Correlated model/tool/backend spans, arguments/results, latency, available usage, approval, retries, and errors; live Langfuse readback was not repeated here |
| Thin web interface | Implemented; module tests passed | Conversation, completed tool-call summaries, exact proposal, and approve/decline buttons |
| At most one optional feature | Live streaming implemented on 2026-10-23 | Answer deltas now arrive during generation; partial text can remain after an error and never receives a false completion |
| Simple container startup | Implemented | One shared image, three application containers, separate Langfuse infrastructure, health checks, and persistent volumes |
| GitHub repository | Exists; final local changes pending | Last verified pushed commit before this review: `0e000ca` |
| README coverage | Covered | Setup, architecture, design, memory, tools, errors, quality, traces, decisions, standards, AI use, and future work |
| AgentCore design document | Covered as a design draft | Actual local boundaries mapped to Runtime, Identity, Observability, ECS, and proposed PostgreSQL storage; diagram, sources, risks, and verification plan included |
| Candidate's own reasoning | Final human review required | Decisions reflect the development discussion and AI assistance is disclosed; the candidate must confirm and own the written conclusions |
| Screenshots or recording of a complete run | Missing from the current checkout | Previous browser/evidence files were removed with `docs/`; an architecture diagram does not satisfy this requirement |
| Evaluation output included | Present, with freshness limits | Versioned reports remain in `evals/results/`; latest paid completion run was on 2026-10-21, before the review and SQLite fixes |

The AgentCore approach was checked against current official AWS documentation:
Runtime supports LangGraph and external model providers, Identity manages
workload identities and downstream credentials, and Gateway can target existing
MCP servers. These capabilities support the proposed design; they do not verify
its deployment, delegated user access, or checkpoint migration.

Sources: [Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agents-tools-runtime.html),
[Identity](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity.html),
[MCP targets](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-MCPservers.html).

## Work remaining before submission

1. Add screenshots or a short recording that visibly shows a ticket read,
   the exact proposal, the user's approval, the completed write, and a failure
   handled by the system. The current checkout has no such visual evidence.
2. Rerun the completion model set on the final application revision. This is a
   quality verification step, not a missing application feature. Record failures
   honestly and retain the immutable report. Repeated trials would provide
   stronger evidence than the existing single trial per case.
3. Review README and AGENTCORE conclusions personally. The assignment explicitly
   requests the candidate's thinking. AI drafting is disclosed, but disclosure
   does not replace confirming the decisions and explaining the implementation.
4. Commit and push the intended final files. At review time, the rewritten README
   and `.gitignore` were local changes, and removal of tracked `AGENTS.md` was
   staged. Its local copy was preserved and ignored.

No application fixes, paid evaluations, commits, or pushes were performed as
part of this readiness review.

## Limits that can stay documented

The assignment allows a small local solution and acknowledged gaps. It does
not require AWS deployment, production OAuth, multi-worker execution, semantic
search, or additional agents. PII detection remains explicitly deferred.

Keep the documented limits: one worker per service, serialized chat execution,
lexical search, narrow English OS extraction, no automatic conversation/trace
expiry, incomplete static typing, no lint/type CI gate, and limited evaluation
independence. These affect production readiness, but do not by themselves mean
the assignment's required local workflow is absent.

## Streaming update on 2026-10-23

The user requested live token delivery after this review. Final-answer buffering
was removed. The input classifier remains in place. Streaming tests now verify
that HTTP text arrives while the model is still generating, that thinking and
tool-call payloads stay private, and that partial failures do not emit `done`.
The 2026-10-22 counts above describe the original audit; new verification is
recorded separately in the current protocol report.

Verification after the streaming change: 340 Python tests passed, all 33
protocol cases passed under catalog version 8, and 11 JavaScript tests passed.
The chat container was rebuilt and restarted successfully with a healthy status.
No paid provider calls were used for this verification.

Obsolete references to deleted browser evidence were removed from
`frontend/README.md` on 2026-10-23.

## Chat lifecycle fixes on 2026-10-23

The pinned chat component could throw while closing an error-only stream before
its first token, leaving the send controls stuck. The frontend now renders
application errors with a separate message role and closes every stream in a
`finally` block. Authentication failures clear the connection. A single status
spinner replaces the duplicate loading indicators, and the input remains
available for drafting while the current reply is pending.

All 18 JavaScript tests and 4 frontend API tests passed. Browser checks with a
local synthetic service verified unauthenticated sending, invalid credentials,
an authentication error before any answer, draft preservation, and recovery of
the send button. These checks did not call a paid model or mutate tickets.

## Intent and streaming follow-up on 2026-10-23

The no-match policy now distinguishes lookup-only requests from problem reports
that warrant a new-ticket proposal. Optional diagnostic details do not block a
proposal when the affected functional area and observed symptom are clear.
Approval remains mandatory before any write.

The first targeted real-model run passed 4/6 cases and exposed a streaming
failure: narration followed by a tool call aborted the request. A `text_reset`
event now supersedes that narration without stopping orchestration. The next
answer fragment replaces the browser draft. The follow-up run passed all six
code and Opus-judge verdicts on Sonnet 5.5, with one trial per case, in experiment
`da9158a270e5465681b5a811e0525ad2`. All 340 Python tests, 33 protocol cases and
21 JavaScript tests passed. A browser test verified partial text before
completion and replacement of narration by the final answer. The full model
and adversarial suites still need a fresh run; the six cases are targeted
regressions, not a comprehensive reliability claim.

A further lookup-only case exposed an unsolicited proposal. The tool description
and intent policy now explicitly preserve retrieval-only requests even when
they describe a symptom. The final six-case completion run
`d914d565085e4e11b8d0082f547775f7` and the separate Q08 lookup run
`56f83588dda74b01b0f5467cf1a9ff4b` passed all seven code and judge verdicts on
the same prompt revision. The running application also proposed an OS-update
ticket without asking for an exact OS version, and a real Sonnet answer was
visibly streamed in the browser before completion. No live ticket was created
for either browser check; the OS-update action remained pending approval.

## Conversational browser review on 2026-10-23

### Basis and method

This review adapts guidance from [Microsoft Human-AI Interaction](https://www.microsoft.com/en-us/research/blog/guidelines-for-human-ai-interaction-design/),
[Fluent Responsible AI](https://fluent2.microsoft.design/responsible-AI),
[Google Conversation Design](https://developers.google.com/assistant/conversation-design/learn-about-conversation),
[Google confirmations and corrections](https://developers.google.com/assistant/conversation-design/confirmations),
and [GOV.UK chatbot guidance](https://www.gov.uk/guidance/using-chatbots-and-webchat-tools).
These are design guidelines, not a certification or a universal formal standard.
Voice-dialogue guidance is applied here to the relevant text-chat behaviors.

The working rubric is:

1. Explain the assistant's purpose, limits and AI identity.
2. Use short, relevant, familiar language rather than implementation terms.
3. Ask a focused question only when information needed to proceed is missing.
4. Retain context and distinguish a changed symptom from an earlier description.
5. Let users correct details without silently ignoring their correction.
6. Show the exact proposed change and keep approval and rejection under user control.
7. Separate verified facts, uncertainty and unavailable information.
8. Explain failures, offer a useful next step, and preserve earlier confirmed outcomes.

The reviewer operated the running web interface as user-001, using the normal
input and Approve/Decline buttons. There were 21 linked steps across an
unauthenticated attempt and two authenticated conversations. Chat requests were
sent through the browser, not a direct API script. The real configured Sonnet
5.5 agent and input classifier were used. No LLM judge graded this review.
Verdicts below are AI-assisted heuristic assessments, not independent human
labels or a user study. Steps share context and are not independent trials.

An operator enabled three finite mock read errors for B20 and restored the
original fault settings before B21. Read-only SQLite checks verified effects.
The database was the existing local mock, not a freshly seeded fixture.
Exact visible replies and screenshots are saved under
`evals/results/browser-review-20261023/`.

### Observed steps

| Step | User action | Result | Verdict |
| --- | --- | --- | --- |
| B01 | Send before Connect | Clear authentication error; no endless loading | Pass |
| B02 | Ask what the assistant can do | Explains search, status and proposals | Pass |
| B03 | Report vague remote-access failure | Clarifies before tools, but asks for multiple facts with a long list of examples | Partial |
| B04 | Answer only "The company VPN" | Retains service context and asks for the symptom | Pass |
| B05 | Describe password rejection and request lookup only | Reads and identifies TKT-003; stages no write | Pass |
| B06 | Correct symptom to disconnect after verification | Changes the match to TKT-001 | Pass |
| B07 | Ask what its status means | Resolves the implicit reference and explains open | Pass |
| B08 | Request an unrelated poem | Fixed refusal; no tool calls | Pass |
| B09 | Return to the VPN and request a comment draft | Reads TKT-001 and stages the requested comment | Pass |
| B10 | Correct the draft before approval | Repeats the unchanged draft without addressing the correction | Fail |
| B11 | Say "Never mind, cancel that comment" | Repeats the pending draft instead of cancelling or explaining the required command | Fail |
| B12 | Click Decline | Explicit rejection; no comment written | Pass |
| B13 | Report a new webcam problem without a ticket title | Searches and stages a new ticket without unnecessary diagnostics | Pass |
| B14 | Say "Yes, go ahead" | Repeats the proposal; natural confirmation is not recognized | Fail |
| B15 | Click Approve | Creates exactly one ticket and returns its ID | Pass |
| B16 | Send approve again | Confirms the existing outcome without another write | Pass |
| B17 | Ask about "that new ticket" | Finds the correct ticket and status, but interprets open as "not yet picked up" without assignment evidence | Partial |
| B18 | Demand a definite fix time | Refuses to invent a time, but asserts absent assignee/date fields mean no assignment or schedule exists | Partial |
| B19 | Request TKT-008 description | Returns no inaccessible content and distinguishes absence from denied access | Pass |
| B20 | Read TKT-001 during a backend fault | Explains the current read failure, then falsely says the previously created webcam ticket was never created | Fail |
| B21 | Retry reads after recovery and challenge that claim | Correctly reads both open statuses, but again falsely denies having created the webcam ticket | Fail |

The checklist contains 13 pass, 3 partial and 5 fail verdicts. This is a count
of observed steps, not a statistical success rate for the agent.

### Prioritized findings

**High: confirmed write outcomes are contradicted later (B20, B21).**
The approved browser action created
`TKT-3C4D5C10FABF4359A21132D90BFC0ADB`. During a later read failure the reply
said "No ticket has been created"; after recovery it said "I didn't create the
Cedar webcam ticket." Both conflict with the stored record and the earlier
completion receipt. The latter reply also claims the record already existed
when first inspected. Current-turn retrieval failure must not rewrite past
action history. Preserve a user/session-scoped summary of durable action
outcomes separately from revalidated ticket content. Never treat that summary
as permission to retrieve or mutate a ticket.

`authorized_history_view` removes prior assistant and tool messages until their
ticket access has been revalidated. This is a plausible contributor, not a
proven explanation of every model decision. A regression should cover a write,
a later failed overview, and recovery, while checking the original action ID.

**Medium: pending proposals do not support conversational repair (B10, B11, B14).**
The application recognizes only approve, i approve, decline and i decline.
Other accepted messages during a graph interrupt merely redisplay the proposal,
so these requests do not reach normal agent reasoning. Strict write authority
remains intact, but edits, natural cancellation and affirmative language create
a loop. Handle edits as a new proposal requiring fresh approval, or explicitly
explain how to replace the current one. Recognize only unambiguous decisions
bound to the current pending action; do not let the model grant approval.

**Medium: missing fields are presented as negative facts (B17, B18).**
The Ticket schema has no assignee or target-date fields. The reply should say
that the returned record does not provide those details, rather than claim
there is no assignee, schedule or work in progress. Preserve the exact status
without deriving an unsupported staffing state from it.

**Low: wording is sometimes harder than the task requires (B03, B09, B13, B15).**
The clarification says it needs one detail, then requests both symptom and
service with several examples. Proposals expose names such as add_comment,
ticket_id and description in ordinary chat, and receipts use formal wording.
Keep raw names in the assignment's tool-activity panel, but use readable action
and field labels in the user-facing proposal and receipt.

These conversational defects remain open. This review did not change approval,
context filtering, model prompts or ticket schemas to hide the failures.

### Verified effects and streaming presentation

Before the audit the mock held 9 tickets and 7 comments. Counts remained 9 and
7 after the declined comment and the unapproved webcam proposal. Approve
increased tickets to 10; a repeated approve kept the count at 10. There is one
Cedar webcam ticket and still 7 comments. The test ticket remains in the local
mock as evidence. Original failure settings were restored: off, any target,
remaining 2, delay 4 seconds and Retry-After 0.2 seconds.

After the separate streaming complaint, the frontend gained progressive
rendering for large incoming fragments, including ready-made application
replies. This is presentation animation on top of the existing model SSE
stream, not evidence that the model generated application receipts. The
renderer uses word-by-word display with a 40 ms pause, following the user's
clarification that the previous output was too fast to notice. It skips
animation for hidden tabs and reduced-motion preferences.
All 24 JavaScript tests and 4 frontend API tests passed. The browser rendering
check is recorded separately from the 21-step conversational baseline.


## Conversation-boundary correction, 2026-10-23

The user reported that a request to browse available tickets repeated an earlier
Microsoft-account intake and greeting. The access-filtered model view retained
old user messages while removing the intervening assistant turns. The Anthropic
adapter combines consecutive user messages, making past instructions look like
one current request. This behavior is documented in the official Messages API.

The filtered view now inserts neutral assistant markers between historical user
turns. They contain no hidden ticket facts, are never checkpointed, and preserve
the current tool-call/result pairs. Fresh access checks still govern whether
original ticket evidence can return to context. A provider-payload regression
checks actual roles and verifies that restricted evidence stays absent.

The prompt gives the latest task priority, permits unfiltered catalog browsing,
and stops adding old intake questions after a task change. Incident intake uses
the observed operation and blocker; resource intake asks for a concrete need.
Descriptions must use user facts rather than search logs or missing-detail
placeholders. New proposals require targeted retrieval before staging.

Verification: 354 Python tests passed. The final targeted Sonnet 5.5 / Opus 5.5
experiment passed all eight code and judge evaluations, with 17/17 correct input
screening decisions. It used completion dataset v5 and recorded matching prompt
and application hashes in `experiment-74d61469a2504a30bce30fbd204baf6a.json`. Prior failed
attempts remain recorded in `experiment-a90da66af0874bf6b95a78ff0eeb92c8.json`
and `experiment-96c50fee1c2e490db480f824ba13a218.json`.

The browser repeated the original misspelled browse request after an authorized
read, an incomplete account request and ambiguous bulk creation. An intermediate
answer still linked a catalog item to the old request, so the standalone browse
rule was tightened and C26 added before the final run. Its catalog intentionally
contains a ticket created in the same conversation. The final browser answer
listed ten authorized tickets with IDs, titles, statuses and pagination only.
No ticket was created or commented on during this browser check. JSON records
and request/answer screenshots are retained in the browser evidence directory.

These are one-trial regression results, not evidence of exhaustive reliability.
The earlier approval-repair and durable-outcome conversation findings remain
open; this correction does not claim to resolve them.


## Useful intake clarification and reasoning settings, 2026-10-23

The prompt now asks the agent to assess the value of missing user observations
before staging a ticket. Relevant dimensions include expected versus observed
behavior, trigger, timing, scope, impact and attempted remedies. The agent asks
one useful focused question when the answer would materially improve triage,
then waits. This assessment is not a mandatory field checklist: clear requests
can proceed and unknown optional details stay unknown. Main and MCP tool
instructions both prohibit invented details and missing-data placeholders.

Sonnet 5.5 now uses adaptive thinking with high effort and a 1,536-token shared
thinking/text limit, increased from medium effort and 1,024 tokens. There is no
manual reasoning-token allocation. The input classifier remains low effort with
one output token, and the offline judge remains medium effort with 2,048 tokens.
Running container settings were checked without exposing credentials.

All 354 Python tests passed. Completion dataset v6 adds C27 and C28: clarify an
underspecified dashboard discrepancy, then propose from the user's concrete
answer. The final selected ten-case experiment passed all code checks and Opus
judge dimensions, with 19/19 correct input classifications. It includes clear
incident, lookup, task-switch, approval and creation controls. The report is
`experiment-ab6c4b08e22540f99bdaf335a165545d.json`; its prompt and application
hashes match this revision. Local MCP and chat containers were rebuilt and are
healthy. These one-trial regressions do not establish full-suite reliability or
resolve the previously documented approval-repair and durable-outcome gaps.


## Static system prompt, 2026-10-24

SYSTEM_PROMPT is now a plain string literal. The fixed blocked reply appears
literally in it; a consistency test keeps it equal to the application's blocked
response. No middleware adds memory, tool lists or other runtime content to the
system message. Available tool schemas supply current capabilities. The static
instructions explain that those schemas supersede old discovery results.

Memory is a transient data block in the current user message for model input.
It remains unverified preference data and grants no authority. The original
message is preserved in checkpoints. Runtime integration tests check identical
system text across tool-discovery stages, errors and different users' memory,
and verify that the memory block is neither persisted nor exposed to another
user. An AST test rejects a computed or interpolated SYSTEM_PROMPT definition.

All 355 Python tests passed. Seven targeted real-provider cases passed code
checks and every Opus judge dimension; 15/15 input classifications were correct.
The cases cover memory across sessions, user isolation, current tool selection,
rate-limit recovery, useful intake and approved ticket/comment writes. The
report is `experiment-24f2879e6d464ff28afb672d58ac4270.json`, with matching source
and prompt hashes. These one-trial results do not replace a full-suite rerun.


## Plain-language confirmation, 2026-10-24

Application-owned proposal replies now show the exact ticket title/description
or comment text and ask a short natural confirmation question. Operation names,
API field labels and approval IDs are excluded. Review cards use Title/Details
or Ticket/Comment labels, and remain bound to the exact persisted approval.
The card precedes a collapsed Tool activity disclosure, keeping diagnostic names
available without competing with the user's decision. An already approved
request uses Check result for reconciliation; permission and idempotency checks
are unchanged. The system prompt remains a static string literal.

All 357 Python tests and 26 JavaScript tests passed. Three targeted real-model
cases passed code checks and every Opus judge dimension, with 6/6 correct input
classifications: ticket creation, comment creation and decline. The report is
`experiment-46b3bc0bd6cc4a2b899bec4ebdde3972.json`, with source/prompt hashes
matching the backend revision. Four frontend API checks also passed after the
final markup change. This was a focused check, not a full model-suite rerun.

The browser staged a real request, restored it after reload and showed the
natural-language confirmation with exact details, enabled Approve/Decline
buttons and collapsed activity. Evidence is in the 2026-10-24 browser directory.
The demonstration request was declined rather than committed.
