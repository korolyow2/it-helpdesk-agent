# Measured auxiliary fallback evaluation — 2026-10-21

**Latest full v5 result: 54/54 joint code/judge passes.** One new end-to-end trial per frozen case. Dataset labels and code criteria were not changed. Earlier failures and negative judgments remain preserved.

| Suite | Cases | Joint passes | Langfuse experiment |
| --- | ---: | ---: | --- |
| tool-calling | 21 | 21 | [Open](http://127.0.0.1:3000/project/helpdesk-assignment/experiments/results?baseline=6f3c25ecc68dd79d) |
| adversarial | 22 | 22 | [Open](http://127.0.0.1:3000/project/helpdesk-assignment/experiments/results?baseline=0babbc20956bc270) |
| answer-quality | 11 | 11 | [Open](http://127.0.0.1:3000/project/helpdesk-assignment/experiments/results?baseline=8ffc80fdd53293fd) |

## Models and refusal recovery

| Operation | Primary | Reserve after refusal | Output budget |
| --- | --- | --- | --- |
| ReAct agent | Sonnet 5 | None | 1024, adaptive thinking, medium effort |
| Input classifier | Sonnet 5 | Opus 4.8, once | 1, disabled thinking, low effort; plain 0 or 1 |
| Offline judge | Opus 5.5 | Opus 4.8, once | 2048, adaptive thinking, medium effort; native JSON schema |

Sonnet 5 returned an empty allowed-fallback list through the Models API. The implementation therefore follows Anthropic's [client-side refusal recovery](https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback), using the documented Opus 4.8 reserve. Only `stop_reason=refusal` switches models. Invalid output, transport errors, timeouts and a second refusal never start another model chain. Caller validation, SDK retry limits and request deadlines remain in force.

The judge model change was explicitly selected by the user. Sonnet and Opus still share a provider/family; this is not independent human evaluation. Changes in joint scores include a judge-model change.

## Measured behavior

- Code: **54/54**. Judge available: **54/54**; all five dimensions passed on **54/54** available judgments.
- Binary routing: **58/58** expected user turns, false blocks 0, false allows 0.
- All 20 classified blocks returned the exact static response, made no agent/MCP calls and left checkpoint IDs unchanged.
- Guardrail reserve invocations: **1**; judge reserve invocations: **0**. The primary refusal and the reserve decision remain separate observations with their actual model and usage.
- Agent-trial latency: p50 **7.40 s**, p95 **15.57 s**, excluding offline judging.
- 289 Python correctness tests passed. The unchanged frontend parser has a separately retained 8/8 result. No lint/type checker is configured.

## Trace verification

API read-back verified all 54 native experiment items and joint scores, one root per item, no missing parents and only `sdk-experiment` observations. The trace contains 58 screening operations with 59 provider invocations, 54 judging operations with 54 provider invocations, and 134 main-agent generations. All 32 retrieval turns began with the main model before MCP.

`guardrail.input` and `judge.answer` are logical spans without generation usage or inferred model cost. `guardrail.attempt` and `judge.attempt` generation children own their respective model usage. The API proof verifies no duplicate parent usage. Canonical agent output includes reasoning tokens; normalized SDK output can categorize reasoning separately. Billed cost and physical provider HTTP retry counts are not inferred. Configured-secret scanning found no leaks in the inspected artifacts.

## Preserved regressions and calibration

- [Pre-fallback summary](summary-before-auxiliary-fallback.md) retains the 53/54 result and earlier prompt failures. Its judge used Sonnet 5.
- [First Opus 5.5 full run](experiment-306687ad82934097856bc65a3e01ae4d.json): 54/54 code, 53/54 joint. The judge flagged an unsupported access limitation in Q02. [Historical API proof](../../docs/evidence/auxiliary-fallback-306687ad82934097856bc65a3e01ae4d.json) preserves the original result.
- [First Q02 repeat](experiment-c5ea649137834d92990ed8bcefc63248.json): 1/2 joint; both judgments passed but one code check caught a paraphrased status. The final prompt distinguishes missing findings from denied access and preserves exact returned status values. [Corrected Q02 repeat](experiment-d3fbf707acba4d05819b4c2a88f0159d.json): 2/2 joint.
- [Preliminary A04 smoke](experiment-dfa02aa815474b4380f05fa67ad922b4.json): 2/2 joint, recorded separately from the final full run.
- [Opus 5.5 calibration](judge-calibration-opus-5-5-2d6e075c98c144b0.json): 9/9 synthetic anchor agreements. These labels are AI-assisted and inspected, not independent human ground truth.
- Every case has now been inspected; historical held-out labels do not make this an unseen benchmark. A single final trial per case is not a reliability or security guarantee.

## Artifacts and reproduction

- [Full report](experiment-52f36aedcf6644adbc51e432006d2fac.json) and append-only `.jsonl` journal.
- [API verification](../../docs/evidence/auxiliary-fallback-52f36aedcf6644adbc51e432006d2fac.json).
- [Verified fallback trace](http://127.0.0.1:3000/project/helpdesk-assignment/traces/eab8cf8ba9d232d77ba142ba94755f99): claude-sonnet-5 → claude-opus-4-8.

```sh
uv run --env-file .env --env-file .env.langfuse python -m helpdesk.evaluation
uv run python -m pytest backend/tests
```

The raw report freezes models, settings, fallback policy, prompt/dataset/source hashes and dependency-lock hash. Final source and prompt hashes match the API-verified run. Rerunning incurs provider usage.

The conversational agent remains read-only. Conversational writes/approval, cross-session facts, application containers, the runtime fault switch and AWS design remain pending. PII detection is deferred. Passing this inspected finite suite does not establish complete assignment delivery or production safety.
