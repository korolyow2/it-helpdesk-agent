# Assignment status and prompt review

Reviewed on 2026-10-21 against the complete source document,
[Build an agentic system](assignment/AgentCore_Platform_Engineer_-_Assignment%20%281%29.docx),
and the actual application. The authorized implementation batch is complete.
The AWS design and candidate-reviewed submission narrative remain separate work.

## Requirement coverage

| Source requirement | Verified implementation | Remaining work or limit |
| --- | --- | --- |
| HTTP user/session/message service | Authenticated `/chat` and answer-only `/chat/stream` | Trusted local developer CLI is separate from HTTP authentication |
| Configurable provider | LangChain configuration; Sonnet 5.5 installed/evaluated | Another provider needs its integration and credentials |
| Authenticated MCP/mock boundary | Five Python tools; independent service and user authorization | Local token scheme; production OAuth outside scope |
| Paginated search and status | Permission-filtered overview, keyword/ID search and detail read | Literal OR search, no embeddings/synonyms |
| Propose ticket/comment and stop | Model-selected write, exact durable proposal, native `interrupt` | One action per session review; edits require a new proposal |
| Later explicit approve/decline | Application-only decision bridge and `Command(resume=...)`; exact user/session/action | Ambiguous acknowledgements do not approve |
| Restart/idempotency/uncertainty | Native checkpoint plus durable binding; atomic backend mutation/replay; repeated approve adds one mutation | Exactly-once mutation per action, not exactly-once HTTP delivery |
| Session history and isolation | User/session-hashed SQLite checkpointing | No automatic history TTL/cleanup |
| Useful cross-session fact | Explicit operating-system preference, user-scoped, last assertion wins, 30-day TTL, inspect/delete API | Narrow English extractor; deletion does not erase previous chat/traces |
| Runtime failures and bounds | Operator-only finite 429/503/slow/committed-timeout switch; retries/backoff/deadlines | Single-worker fault counter resets on restart |
| One-command evaluations | 337 passing Python tests; 33 protocol mappings; new frozen 17-case actual-model completion suite and Opus judge | AI-assisted labels, same-family judge, independent human review pending |
| Traces and available usage | Correlated guardrail/model/MCP/mock/approval spans; read-back of all 17 native joint scores | No claim of unavailable HTTP retry counts, billed cost or complete per-dimension export |
| Thin page and review buttons | Exact proposal panel, restore on reconnect, bound approve/decline, separate tool summaries | Visual polish not prioritized |
| One optional feature | User-facing answer streaming | No handoff/cancellation feature added |
| Containerized local startup | Non-root locked image; healthy mock/MCP/chat; named volumes; actual network/provider smoke and restart | Langfuse remains separate; native Linux host-network caveat documented |
| README and decisions | Current startup, architecture, trust, memory/retention, failures, evaluations, AI-use limits | Candidate must review personal architecture/engineering/AI-use conclusions |
| `AGENTCORE.md` and diagram | Not implemented | Write the actual-build AWS design against current official documentation; no AWS deployment needed |
| Evidence and evaluation output | [Completion report](COMPLETION.md), browser screenshots, native experiments, container restart/approval/failure smoke | Candidate selects final submission narrative/recording as appropriate |
| GitHub repository | User-created `korolyow2/it-helpdesk-agent`; publication of completion, review corrections and CI authorized on 2026-10-22 | Verify the remote commit and first hosted CI run after push |

All five requested completion items are implemented. This does not make the
entire assignment ready for final submission while the AWS design and personal
review remain. OAuth, semantic search and PII detection are not explicit source
requirements. PII detection remains an agreed documented follow-up; credential
masking and typed errors are not general PII protection.

## Review corrections — 2026-10-22

The four confirmed Astra defects are fixed with targeted regressions and full
protocol verification. See [the review correction report](REVIEW_FIXES.md).
The old 17-case provider experiment retains its original source hash and date;
it was not rerun on this revision. Final answer text is buffered until validation,
so the SSE interface no longer promises live provider-token delivery. The
remaining submission work below is unchanged.

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
These historical checks supplement the manual review and cannot prove generalization.
The [completion prompt audit](evidence/prompt-review-completion.json) records the
current instruction/hash/import checks after adding proposals and user facts.

The agent retains general product agreements: clarify an underspecified issue,
let the model select discovery, use the overview before other ticket reads,
verify a specific ticket through its detail tool, ground claims in successful
results, preserve returned status values, and keep authority in application code.
These rules can be tested but do not identify a particular fixture. The fixed
English refusal text is an explicit product contract. The current prompt describes
proposal-only model authority, native pause, later application-owned review,
exact approved execution and honest uncertain outcomes. It contains no approval
case IDs or expected ticket contents.

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

## Remaining submission step

Prepare `AGENTCORE.md` with the AWS component map, diagram, documented risks and
explicitly verified assumptions. Then review the candidate's own engineering
choices and AI-use statement and select the final evidence. These are not
implemented by the completion batch; do not invent personal experience or claim
AWS deployment validation. The user authorized publishing the current changes
on 2026-10-22; verify the resulting remote commit and hosted CI separately.
