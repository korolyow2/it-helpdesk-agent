# Review corrections — 2026-10-22

All four reproduced Astra findings are fixed. The corrections preserve the
existing create_agent graph, model configuration, backend authorization and
human-approval boundary. No new dependency, agent, model generation, commit or
push was introduced for this work.

## Corrected behavior

| Finding | Correction | Regression evidence |
| --- | --- | --- |
| Revoked created-ticket data reappeared in model context | Include top-level IDs, listing items, nested `ticket.id`, and comment `ticket_id` when revalidating historical evidence. A partial overview cannot release unseen IDs. | Create through MCP, approve, restrict the new ticket, reload overview and inspect actual subsequent model input. |
| Intermediate model text broke SSE completion | Hold fragments per model message until graph completion; publish only the validated final message or application-owned proposal. Concatenated deltas must equal `done.reply`. | Text-plus-tool-call followed by an answer; narrated write followed by interrupt; unfinished/truncated provider output; gated HTTP delivery. |
| Staging crash stranded the backend proposal | Resume the original durable tools checkpoint using `None` and a tools-only breakpoint. The backend deduplicates staging; the local binding and native interrupt are restored. Never run a new model or an approved mutation during recovery. | Crash before/after local binding, reopen service, recover through review/message, check native interrupt, explicitly approve once. Retry temporary recovery failures; recheck membership. |
| Reconciliation lost write uncertainty | Use the same typed MCP transport mapper for normal tools and reconciliation. Persist `unknown` before approved dispatch; replace it only with validated results. A review lookup can confirm completed backend state without tool execution. | Three pre-write 503s, explicit retry, real backend commit plus simulated MCP timeout, restart, completed confirmation and unchanged mutation count. Also lose local completion after commit. |

Recovery is scoped to the same authenticated user/session and the exact
model-selected call already held by SQLite checkpointing. A recovered proposal
is shown before a later human decision. GET review lookup may repair that
unapproved proposal's staging; it never grants approval or executes a decided
action. During recovery a transient staging error is raised before the tools
node completes, preserving its retryable checkpoint rather than advancing to
a model. Temporary context flags reset on exit.

## Streaming tradeoff

A model can produce text and only later select a tool. Without another model
phase, there is no reliable way to publish that text live and guarantee that it
belongs to the final answer. We retain the single-agent/single-answer path and
buffer text until final-message validation. The SSE interface then delivers the
final fragments and an exact matching completion. This delays first visible
text until generation completes; it is not live provider-token delivery.
Thinking, intermediate narration, tool results and failed provisional answers
never enter public deltas. A later network delivery failure remains an incomplete
stream and is rejected by the existing browser parser.

## Verification

- **337 Python tests passed**, including **11 new regression instances**.
- **33/33** protocol requirements passed: P01–P20, F01–F09 and R01–R04.
- **11/11 JavaScript tests passed**; the strict SSE consistency check remains.
- [Current protocol report](../evals/results/latest-protocol.json) records current
  application/test/lock/catalog hashes. Tracing was disabled for this local run.
- [Machine-readable correction evidence](evidence/review-fixes.json) records
  coverage and the rebuilt local container's source/health verification.
- `git diff --check` passed. No lint or type checker is configured.

The first protocol mapping run passed all 337 tests but reported one missing
selector because a strengthened streaming test had been renamed. Its report is
preserved as `protocol-review-fixes-selector-mismatch.json`. Catalog v7 updates
that selector and adds the four regression groups; the complete rerun passed
all 33 mappings. The original v6 catalog and pre-review protocol report remain
in the archive. Failed checks were not silently replaced.

No paid model calls were made for these fixes. The previous **17/17** code/judge
and **30/30** input-classifier results are historical evidence for the prior
source revision, preserved in [the earlier summary](../evals/results/summary-completion-before-review-fixes.md).
They do not demonstrate model behavior on the corrected source. Deterministic
models exercise the real graph, MCP and mock; they are not provider quality
measurements. Existing assignment gaps and documented PII deferral are unchanged.
