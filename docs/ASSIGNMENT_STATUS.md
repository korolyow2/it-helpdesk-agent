# Assignment status and prompt review

Reviewed on 2026-10-21 against the complete source document,
[Build an agentic system](assignment/AgentCore_Platform_Engineer_-_Assignment%20%281%29.docx),
and the application entry points. The implementation is a working read-only
Helpdesk assistant with independently tested backend write primitives. It does
not yet complete the assignment's conversational write and memory requirements.

## Requirement coverage

| Source requirement | Verified implementation | Remaining work |
| --- | --- | --- |
| HTTP service accepting user, session and message | Authenticated `/chat` and `/chat/stream` in `backend/src/helpdesk/api.py` | Connect the write and next-message approval path |
| Configurable model provider | `model_from_env` uses `MODEL_PROVIDER` and `MODEL_NAME` | Only Anthropic is installed and evaluated; other providers need their integration and credentials |
| Mock behind an authenticated MCP server | Five tools in `mcp/server.py`; separate mock service/user credentials and backend authorization | No missing read-tool boundary; writes remain unavailable to the conversational agent |
| Paginated search and ticket status reads | Permission-filtered overview/search and detail tools; `MCPTools.READ_TOOLS` gates discovery | Literal keyword search is limited; a semantic search system is not required by the assignment |
| Propose a new ticket or comment, then stop | MCP write tools stage durable proposals; SQLite binds exact action, user and session | Let the agent propose either operation, expose the pending proposal in chat, and stop execution |
| Approve or decline in the next message | Mock has application-only decision endpoints | The agent service must accept the user's explicit next-message decision and resume the exact approved action; never let the model authorize it |
| Approval survives restart and writes never duplicate | Backend persistence, durable idempotency and committed-write timeout tests exist | Verify these through the actual chat workflow, including restart, decline, repeated approval and uncertain outcome |
| Conversation memory within a session | User/session-scoped SQLite checkpoints with restart and isolation tests | Define history retention and cleanup for delivery |
| At least one useful fact across sessions | No user-fact store or extraction/update path exists | Choose a small fact schema, update/retention rules and explicit user isolation; a new session must actually retrieve the fact |
| Mock failure switch, sensible retries and timeout | Bounded HTTP retries, Retry-After, request deadlines and fault-injecting test/eval transport | Add a documented runtime switch to the running mock for rate limits, server errors and slow responses; demonstrate the chat behavior |
| One-command evals including tools, approval, scope and failures | Protocol runner and native Langfuse experiments; frozen read-only model suite and LLM judge | F01–F09 in `evals/cases.json` remain pending; real-model conversational writes, memory and browser approval are not validated by backend tests |
| Request traces with models, tools, latency, tokens and errors | Local Langfuse; correlated MCP/mock spans, available provider usage and guardrail decisions | Add complete conversational proposal/decision/write evidence once that flow exists; do not infer unavailable provider retry counts |
| Thin page showing tools and approve/decline | Plain HTML/JavaScript chat, answer streaming and separate completed tool panel | Add proposal display and approve/decline controls tied to the authenticated pending action |
| At most one optional feature | Answer-only streaming implemented | Keep this as the selected option; no additional agent handoff or cancellation feature is needed |
| Preferred containerized local solution (also agreed project delivery) | `compose.langfuse.yaml` runs tracing infrastructure | Add application containers and a reproducible startup for mock, MCP and chat, with persistent application data |
| README and engineering/AI decisions | Setup, architecture, tools, traces, current evals and limitations documented | Consolidate framework/model alternatives, decision rationale, engineering standards, AI contributions and future improvements with the candidate's own conclusions; reconcile historical progress text |
| `AGENTCORE.md` with a diagram based on the actual build | File is absent | Write the AWS design using current official documentation; distinguish verified capabilities from assumptions; deployment and an AWS account are not required |
| Screenshots/recording and eval output | Read-only frontend screenshot, reports and trace evidence exist | Capture one complete read → proposal → approval → write run and failure handling; include the final eval output |
| GitHub repository | Local Git repository on `main`; the user created `korolyow2/it-helpdesk-agent` and authorized the initial commit and push | Publish the reviewed source, then complete the remaining assignment and final submission evidence |

OAuth, semantic/vector search and PII detection are not explicit requirements in
the supplied assignment. The current local token scheme is documented. PII
detection remains an explicitly agreed, documented follow-up; masking configured
credentials is not general PII protection.

## Prompt audit

Inspected the agent system prompt, guardrail prompt, offline judge rubric,
dynamic discovery/current-tool instructions, and MCP server/tool descriptions.
Runtime prompts do not import evaluation datasets, labels or graders. No seeded
ticket IDs, user IDs, fixture titles, expected answers, case IDs or attack
canaries were found in those instructions. Schema constraints and advertised
tool names describe the implementation rather than a case-specific answer.

The input guardrail did contain few-shot examples introduced while investigating
known evaluation failures. Their identifiers were generic, but their scenarios
closely resembled inspected cases. They have been removed. The replacement is
an ordered policy: authority overrides take precedence; genuine IT actions and
conversational follow-ups are allowed; benign mixed requests keep their IT
component; technology used only as a pretext does not establish a support task.
There are no example utterances or lookup tables in that prompt.

[Static review evidence](evidence/prompt-review-sonnet-5-5.json) records inspected
instruction sources, fixture-marker/import checks and prompt/dataset hashes.
These checks supplement the manual review and cannot prove generalization.

The agent retains general product agreements: clarify an underspecified issue,
let the model select discovery, use the overview before other ticket reads,
verify a specific ticket through its detail tool, ground claims in successful
results, preserve returned status values, and keep authority in application code.
These rules can be tested but do not identify a particular fixture. The fixed
English refusal text is an explicit product contract. The current read-only
capability statement is accurate and must change with the approved write step.

The judge intentionally receives each case's reference expectations and actual
tool evidence. These are grading inputs, never the agent's system prompt or
history. Its generic rubric and strict literal checks have limitations; an
LLM judgment is not independent human ground truth.

Removing examples does not establish absence of overfitting. Both the old and
new prompts were written with knowledge of the regression suite. All 54 v5 cases
have been inspected, so their results describe regression behavior, not unseen
generalization. Preserve failures and frozen labels. Fresh, independently
reviewed tasks and paraphrases remain necessary before a broader quality claim.

## Model migration

The agent and primary input classifier now select `claude-sonnet-5-5` by default,
including the local environment and credential-free example. The judge remains
`claude-opus-5-5`. Agent settings remain adaptive thinking, medium effort, 1024
output tokens, 30-second provider timeout and two SDK retries.

Sonnet 5.5 rejects `thinking: disabled`. The tool-free classifier instead uses
`between_tools`, low effort and one output token; without tools it does not
perform up-front thinking. On refusal, the existing Opus 4.8 reserve uses its
supported `disabled` mode with the same prompt and one-token cap. This preserves
plain binary output and bounds without sending an incompatible model parameter.
See the [official migration guide](https://platform.claude.com/docs/en/models/sonnet-5-5/migration-guide).

The old Sonnet 5 result is preserved in
[the pre-migration summary](../evals/results/summary-sonnet-5-before-migration.md).
See [the current measured report](../evals/results/latest.md) for the exact
model, prompt hashes, results and remaining gaps. A previous 54/54 score does
not validate a changed model or prompt.

## Recommended next agreed step

Connect one complete conversational write path: read an existing ticket,
propose a comment through MCP, show its exact contents, stop, accept approval or
decline in a subsequent user message, and execute at most once after recorded
approval. Reuse the existing durable backend contract. Complete its restart and
uncertain-outcome tests before expanding to ticket creation, user facts or AWS
design. This is a recommendation for the next discussion, not implemented work.
