# Review correction verification — 2026-10-22

All four reproduced review defects are corrected: historical access to created
ticket results, intermediate SSE narration, staging-crash recovery and lost
reconciliation responses. [Correction report](../../docs/REVIEW_FIXES.md).

- **338 Python tests passed**; **33/33 protocol requirements passed**.
- **11/11 JavaScript tests passed**. Strict stream/completion matching is retained.
- Eleven new regression instances cover actual graph/MCP/mock behavior with
  synthetic models, crashes, access changes and controlled failures.
- [Current protocol report](latest-protocol.json) records source/test/catalog/lock
  hashes. This run had tracing disabled and made no paid provider requests.
- [Correction evidence](../../docs/evidence/review-fixes.json) records final
  container source/health checks. `git diff --check` passed; no lint/type checker
  is configured.

Final-only SSE now buffers model text until final-message validation. This
prevents text preceding tool calls from reaching the public stream, at the cost
of first visible text arriving after generation completes. It does not claim
live provider-token delivery or add another model generation.

## Hosted CI scheduling correction

The first hosted CI passed 335/337 Python tests and failed two proposal paths
with SQLite OperationalError. The [original report](protocol-ci-first-run-failed.json)
is preserved. Blocking state I/O on the event loop could prevent an asynchronous
checkpoint writer from committing. State calls in asynchronous paths now use
`asyncio.to_thread`; one deterministic writer-contention regression was added.
The full corrected local run passed 338 tests and 33 mappings without relaxed
timeouts or test retries. [Details](../../docs/CI.md#first-hosted-run-and-sqlite-correction).
The preceding 337-test report is retained in [its archive](protocol-before-ci-lock-fix.json).

## Historical provider results

The [pre-review completion summary](summary-completion-before-review-fixes.md)
retains the actual **17/17** code/judge and **30/30** input-classifier results,
including Langfuse links and preserved failed trials. Those experiments were
not rerun after these source changes and are not represented as current model
quality measurements. The earlier Sonnet migration reports are also unchanged.

## Preserved protocol reports

- [Before these fixes](protocol-before-review-fixes.json): 326 tests and 29 mappings.
- [Selector mismatch](protocol-review-fixes-selector-mismatch.json): all 337 tests
  passed, but one mapping referenced a renamed streaming test. Catalog v7 fixes
  that stale reference and adds R01–R04.
- [Final full rerun](latest-protocol.json): 337 tests and all 33 mappings passed.

```sh
uv run python -m helpdesk.evals --mode protocol
node --test frontend/tests/*.test.mjs
```

The AWS design, candidate-reviewed personal writeup, independent human labels
and production hardening remain separate work. PII detection is explicitly
deferred. Existing evidence does not establish production reliability/security.
