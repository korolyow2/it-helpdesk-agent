# Completion report — 2026-10-21

The authorized batch is implemented: agent-selected ticket creation and comments,
durable next-message review, one retained user fact, browser decisions, runtime
mock faults and application containers. The user authorized publishing this
batch together with the review corrections and CI on 2026-10-22.

## Review corrections — 2026-10-22

Four independently reproduced review defects have been corrected. Current
regression coverage and delivery tradeoffs are in [review fixes](REVIEW_FIXES.md).
The 17-case provider run, Langfuse read-back and browser/container screenshots
below describe the 2026-10-21 pre-review revision, not a new paid model run.
Current protocol results are recorded separately; the previous report is
preserved as `evals/results/protocol-before-review-fixes.json`.

## Implemented behavior

The existing `create_agent` ReAct loop selects a write after authorized reads.
`WriteReview` stages an exact MCP proposal, binds it to user/session/model call,
and invokes native `interrupt`. There is no mutation before a later explicit
approve. The application records the human decision through its non-tool
approval bridge and resumes the same checkpoint with `Command(resume=...)`.
The model-facing schema hides approval IDs. Decline invokes no execution call;
backend approval and live access checks guard execution and replay. Mutation
and its idempotent result are committed together. A repeated approval or a lost
committed-write response cannot create a second mutation for the same action.

Cross-session memory retains only an explicitly stated English first-person
operating-system preference. Last assertion wins; the 30-day TTL refreshes only
on an assertion. Expired facts disappear on access. Authenticated GET/DELETE
`/memory` inspect/delete the user's extracted fact. This does not delete stored
chat history or past traces. Facts never provide authorization.

The review panel displays exact arguments using text rendering. Buttons send a
new approve/decline message bound to the displayed ID; reconnect restores the
same session's review. Only answer text is streamed. An approved uncertain
outcome offers same-action reconciliation, not a new proposal or rollback claim.

The operator-only running mock switch supports finite 429, 503, slow and
committed-response-timeout faults. Authorization precedes injection. The
three-service Docker stack uses non-root processes, named persistent volumes,
health checks, loopback ports and locked dependencies. Langfuse infrastructure
is separate. [README](../README.md) documents startup, API, policies and commands.

## Verification

| Layer | Actual result | Evidence |
| --- | --- | --- |
| Python correctness | 337 tests passed | [Protocol report](../evals/results/latest-protocol.json) |
| Requirement mapping | 33/33 P01–P20, F01–F09 and R01–R04 passed | Same protocol report |
| Frontend code | 11 stream/review tests passed | `node --test frontend/tests/*.test.mjs` |
| Prior real model + judge | 17/17 code and judge; 30/30 input classifications | [Measured results](../evals/results/latest.md) |
| Prior native Langfuse | 17 experiment items and passing joint scores read back; hashes matched at that run | [API evidence](evidence/completion-langfuse.json) |
| Prior browser approve/decline | One approved comment, zero declined comments, exact restored proposals | [Browser evidence](evidence/completion-browser.json) |
| Prior completion containers | Image built; all three services healthy; real proposal/restart/repeated approval adds one comment; runtime 429 recovery | [Container evidence](evidence/completion-containers.json) |

Correctness tests cover new tickets and comments, stop before write, decline,
restart, duplicate/stale/forged approval, user/session isolation, revoked access,
uncertain outcomes, fact overwrite/expiry/deletion, blocked input, malformed
transport errors and finite failures. Real model experiments use isolated
fixtures and the actual graph/MCP/HTTP/mock code through an in-process HTTP
transport; the container smoke separately verifies actual network boundaries.
The browser run uses a scripted model, clearly separate from provider quality.
No stubbed response is represented as Sonnet quality evidence.

The first full completion run failed C13 (16/17) because the MCP transport raced
the backend deadline. Its original report is retained. After bounding backend
work at ten seconds and allowing twelve at MCP, two C13 repeats and the new full
17-case run passed. See the measured report for all original run links and limits.

## Browser evidence

[Pending exact proposal](evidence/completion-review-pending.png),
[completed approval](evidence/completion-review-approved.png), and
[declined proposal](evidence/completion-review-declined.png) show the actual UI.
The approved/declined diagnostic statuses correspond to checked database effects.

## Remaining limits and submission work

This is a small single-worker local assignment, with literal search, finite
synthetic fixtures and narrow English fact extraction. It does not establish
production authentication, multi-worker coordination, broad adversarial safety,
independent human quality or production reliability. Automatic history/approval/
trace cleanup and PII detection are not implemented. User/tool content may reach
the configured provider, local history and traces; secret masking is not PII
protection. Reports preserve failures and AI-assisted labeling limitations.

The AWS AgentCore design/diagram (`AGENTCORE.md`) and candidate-reviewed personal
architecture/engineering/AI-use conclusions remain outside this batch. Review
these before final assignment submission. Container validation is local and
makes no claim of AWS deployment validation. The current source changes require
a separate user instruction before committing or pushing.
