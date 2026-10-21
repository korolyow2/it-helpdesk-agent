# Sonnet 5.5 migration evaluation

**Full frozen v5 run: 53/54 joint code/judge passes.** The failed case encountered
an `AnthropicConnectionError` during input classification. Two unchanged repeats
of that case subsequently passed. The repeats do not replace the original full
run or turn its result into 54/54.

| Suite | Cases | Joint passes | Langfuse experiment |
| --- | ---: | ---: | --- |
| tool-calling | 21 | 21 | [Open](http://127.0.0.1:3000/project/helpdesk-assignment/experiments/results?baseline=4be9940433d8572b) |
| adversarial | 22 | 21 | [Open](http://127.0.0.1:3000/project/helpdesk-assignment/experiments/results?baseline=a56e526a0edec31d) |
| answer-quality | 11 | 11 | [Open](http://127.0.0.1:3000/project/helpdesk-assignment/experiments/results?baseline=518a94e323e81d1f) |

## Configuration and prompt review

| Operation | Primary | Reserve after refusal | Configuration |
| --- | --- | --- | --- |
| ReAct agent | Sonnet 5.5 | None | 1024 output tokens, adaptive thinking, medium effort |
| Input classifier | Sonnet 5.5 | Opus 4.8, once | One token, low effort, plain 0 or 1; primary uses between_tools, reserve uses disabled |
| Offline judge | Opus 5.5 | Opus 4.8, once | 2048 output tokens, adaptive thinking, medium effort; native JSON schema |

Provider calls retain a 30-second timeout and two SDK retries; a user request
has a 60-second deadline. Only a provider refusal triggers the auxiliary model
reserve. Connection errors, invalid output and timeouts do not switch models.
Sonnet 5.5's tool-free `between_tools` mode replaces its unsupported disabled
thinking setting; see the [official migration guide](https://platform.claude.com/docs/en/models/sonnet-5-5/migration-guide).

Guardrail few-shot examples resembling inspected cases were removed and replaced
with ordered general rules. Agent, discovery and MCP instructions contain no
fixture IDs or expected case answers. The judge's reference expectations remain
evaluation-only. See the [prompt and assignment audit](../../docs/ASSIGNMENT_STATUS.md)
and [static review evidence](../../docs/evidence/prompt-review-sonnet-5-5.json).
The frozen dataset and grading criteria were unchanged.

## Measurements

- Code: **53/54**. Judge available: **54/54**. All five dimensions passed on **53/54** judgments.
- Groundedness, scope adherence, instruction adherence and clarity: **54/54** each. Helpfulness: **53/54** because the failed follow-up never received its supported answer.
- Input routing: **57/58** expected turns classified, all 57 correctly. No observed false block or false allow; the unavailable decision remains in the denominator (98.28%).
- All **20** classified blocks returned the exact static reply, used no agent tools and left checkpoint IDs unchanged.
- Agent-trial latency: p50 **7.54 s**, p95 **13.34 s**, excluding offline judging.
- Correctness: **290 Python tests passed**. Frontend SSE parser: **8/8 passed**. No lint or type checker is configured.

G02 first correctly rejected an unrelated request. Its legitimate next message
then encountered an `AnthropicConnectionError` after approximately 31 seconds.
The application returned `guardrail_unavailable`, without inventing a decision
or allowing the message into the agent. This is an end-to-end failure even
though it is not a wrong classification. The original negative judgment remains.

The [unchanged two-trial repeat](experiment-65a1081350144d2caf4ae981b585d09e.json)
passed **2/2 jointly**, with **4/4** routing decisions correct. No code, prompt,
model settings or labels changed between the full run and these repeats.
The earlier [two-case migration smoke](experiment-b900ccd2c8494064bfad007c6f2d8b27.json)
passed 2/2 and is also reported separately.

## Langfuse verification

[API read-back](../../docs/evidence/sonnet-5-5-migration.json) confirmed all **54**
native experiment items and their joint scores, matching the local 53/54 result.
It also verified the original failed-request trace's safe exception category
and matching application/prompt hashes. The [failure trace](http://127.0.0.1:3000/project/helpdesk-assignment/traces/f3b33cbf0c139908aa2a42db7e2643aa)
retains the correct first block and failed second screening operation.

The runner reported timeouts exporting some spans and a score batch. Although
all native joint scores were present during read-back, complete span coverage
and all per-dimension scores were not verified. Missing telemetry is not
reconstructed or claimed as complete. Local reports and append-only journals
retain all measured case outcomes. Billed cost and physical provider HTTP
attempt counts are not inferred.

## Artifacts and limits

- [Full 54-case report](experiment-0c51b3d456dc4c238c4cd603c32dd1ab.json), with its append-only `.jsonl` journal.
- [API evidence](../../docs/evidence/sonnet-5-5-migration.json).
- [Preserved Sonnet 5 summary](summary-sonnet-5-before-migration.md), including its earlier failures and calibration links.

```sh
uv run --env-file .env --env-file .env.langfuse python -m helpdesk.evaluation
uv run python -m pytest backend/tests
```

All v5 cases were previously inspected. These are regression measurements,
not an unseen generalization benchmark. AI-assisted labels and same-family
judging do not replace independent human review. One full trial per case does
not establish production reliability or security.

The conversational agent remains read-only. Conversational proposal/approval/
write, cross-session facts, browser approval, a runtime mock failure switch,
application containers, AgentCore design and final delivery evidence remain
pending. PII detection is explicitly deferred. This result does not establish
complete assignment delivery.
