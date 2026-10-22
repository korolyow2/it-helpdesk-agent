# Local observability

## Policy and sources

There is no universal mandatory "gold standard" logging schema. This project
uses the assignment's trace requirements, Langfuse's observability practices,
and Anthropic's agent engineering guidance. The concrete field names and
approval audit design below are our implementation choices.

- [Langfuse best practices](https://langfuse.com/docs/observability/best-practices):
  meaningful request scope, operation tree, inputs/outputs, model configuration,
  usage, identity, session, and version context.
- [Anthropic: writing effective tools](https://www.anthropic.com/engineering/writing-tools-for-agents):
  inspect tool trajectories; measure task/tool runtime, tool calls, tokens, and
  tool errors. It also cautions against listing whole collections instead of
  targeted retrieval at scale. Our mandatory overview is a small-fixture choice.
- [Anthropic: demystifying evals](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents):
  distinguish the interaction transcript from the actual environment outcome;
  check state rather than trusting a success sentence.
- [Anthropic: API errors](https://platform.claude.com/docs/en/api/errors),
  [stop reasons](https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons),
  and [prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching):
  preserve available provider IDs, termination reasons, and reported usage.
- [Anthropic: multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system):
  production observability need not entail inspecting private conversation content.
- [OpenTelemetry GenAI conventions](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-spans.md):
  useful guidance for inference and tool operations, currently Development.
  We do not claim full conformance to a stable GenAI standard.

## Recorded observations

Each authenticated request starts with `guardrail.input`, a logical tool-free
screening span. Its `guardrail.attempt` generation children record each provider
invocation with the requested/response model, parameters, stop reason, safe
refusal category, available provider ID and reported usage. The parent records
the decision digit, mapped intent, prompt hash, attempt list and duration without
recording candidate text or classifier conversation. It has no generation usage
or inferred model cost, so reserve calls cannot duplicate parent token totals.
The request root starts with `awaiting_screening`;
after a blocked decision it records only a blocked status, with intent in
diagnostic metadata. Neither intent nor static reply enters conversation
history, and the rejected turn creates no checkpoint. Accepted
input remains observable through the normal request and agent generation spans.
Only accepted requests proceed to `model.generate` and model-selected MCP work.
Rejected messages produce no agent/MCP observations. Classifier usage is separate
from agent generation usage; failures remain errors, not successful refusals.
The input guardrail does not implement PII detection or retroactively redact old
traces. Synthetic eval datasets still deliberately contain attack fixtures.

The logical operation's output is assembled by application code, for example
`{"code":1,"intent":"blocked","decision":"block"}`. The LLM itself still
returns one digit with a one-token limit; this logging format does not request
structured model output. Failures instead log
`{"decision":"error","error_code":"guardrail_unavailable"}` with a safe
failure category (`invalid_output`, `provider_error`, or `provider_refused`).
Cancellation records `operation_interrupted` and `interrupted`; it is never
converted into a malicious-input classification. Raw invalid model output and
provider exception text are excluded. Available stop reasons and token usage
remain on individual generation children; duration is measured by their spans.

Only an Anthropic `refusal` triggers one client-side reserve invocation on
`claude-opus-4-8`, preserving the same prompt, format and limits. The current
Sonnet 5.5 classifier uses `between_tools` with no tools; the reserve uses
its supported `disabled` mode. Agent thinking remains adaptive. The original
Sonnet 5 Models API check reported an empty `allowed_fallback_models` list;
that historical finding is not a capability claim about Sonnet 5.5. The client-side pattern is
documented by [Anthropic](https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback).
The primary refusal remains a warning with its actual usage. Invalid output,
timeouts, transport errors and a second refusal never start another model chain.
Final validation still fails closed; no decision or judgment is invented.

The parent `helpdesk.request` mirrors `guardrail_code`, `input_intent`, and
`guardrail_decision` in metadata. Its final status is `completed`, `blocked`, or
`failed`. A correct block is not an operational error; an unavailable check is.
These are routing decisions, not quality scores or proof of attack detection.
Only 0 and 1 are valid under `input-v4-principles`. This revision gives authority
override claims priority while preserving benign mixed IT requests. The safe
provider refusal category is recorded when available; its raw explanation is
not exported. A refusal without a digit remains an unavailable classifier,
never a fabricated successful block. The earlier synthetic evidence
used the previous three-category policy and is retained as historical evidence.
That synthetic four-case read-back verified one trace with 11 observations,
one root, and no missing parents in `protocol-tests`; see
[guardrail evidence](evidence/guardrail-observability.json). No provider calls
were made in that smoke test.

| Boundary | Recorded data |
| --- | --- |
| User request | Request and trace IDs, verified user ID, user-scoped session hash, input, final reply, budgets, deadline, end-to-end duration, first answer token delay, success/failure |
| Context preparation | Approximate input budget, original/selected message counts, context-limit failure |
| Model generation | Effective role-labeled context after trimming, tool descriptions/schemas, prompt SHA-256, provider/model, output limit, timeout, retry configuration, thinking type/effort, result/tool-call IDs, stop reasons, reported usage, available response IDs, duration, first nonempty output chunk time |
| Agent tool | Name, call ID, arguments, result, status, argument correction, local validation rejection |
| MCP server | Discovery/tool operation, access result, request ID, arguments/result when available, duration/error |
| Backend request | Acting user, method/path, validated parameters/body/result, maximum attempts, final safe error and write uncertainty |
| HTTP attempt | Actual one-based attempt number, response status, full streamed response duration, transport failure |
| Retry wait | Error code, next attempt, actual backoff duration, requested Retry-After, idempotent-retry eligibility, accumulated write uncertainty |
| Approval/write | Explicit approve/decline decision, proposal identity, proposed payload, pending/reused proposal, verified approval, committed mutation, durable replay, or error |
| Local eval | Case ID, grader version, checks, verdict, evidence, optional request-trace-linked numeric score |

Durations come from observation start/end timestamps. Counts can be derived from
generation/tool/attempt observations. Request IDs propagate over HTTP; W3C
traceparent propagates to MCP and the mock. MCP per-call metadata is used when
available. The current adapter can bypass FastMCP's per-call propagation, so MCP
operations may be siblings under the request root rather than children of the
corresponding agent tool. Call IDs on the agent side and operation metadata
remain available for diagnosis; a more precise cross-process tool parent is a
known integration limitation.

We use the public Langfuse SDK with explicit observations around our middleware
and service boundaries. We do not add the full LangChain callback tree as well;
that would duplicate observations and capture unnecessary framework state.
FastMCP uses `propagation_only`; the exporter admits our SDK scope only.

Usage comes from provider-reported LangChain metadata. Normalize to Langfuse's
`input`, `output`, `total`, and detailed `input_*`/`output_*` categories, subtracting
exclusive details from their parent category to avoid double counting. Keep raw
reported usage in metadata for audit. Missing usage is absent, never zero-filled.
No application-side tokenizer estimate is submitted as billed usage.

No prices are invented. Langfuse may infer cost only if its model definition and
usage-category prices match. Verify the configured Sonnet model definition before
using cost dashboards. Our code does not prove a price catalog is current.
See [usage and cost](https://langfuse.com/docs/observability/features/token-and-cost-tracking).

One generation represents a logical provider call including SDK retries. The
current LangChain Anthropic integration does not expose actual SDK HTTP-attempt
counts through this instrumentation. The configured maximum and terminal error
are recorded; individual provider retries remain unknown. Backend HTTP attempts
and waits are fully instrumented. Available IDs are stored; unavailable provider
request IDs are not reconstructed from application IDs.

## Start locally

The checked-in Compose file adapts the official
[Langfuse v4 deployment](https://github.com/langfuse/langfuse/blob/main/docker-compose.yml).
It runs Langfuse web/worker, PostgreSQL, ClickHouse, Redis, and MinIO. This storage
is separate from application SQLite. Image tags follow the official major/minor
examples; immutable image digests are not pinned yet.

```sh
uv run python scripts/init_langfuse.py
docker compose --env-file .env.langfuse -f compose.langfuse.yaml up -d
```

Open http://localhost:3000. The ignored, owner-readable `.env.langfuse` contains
the generated initial local login and project API credentials. Existing files
are never overwritten. Initial resources use
[headless initialization](https://langfuse.com/self-hosting/administration/headless-initialization).

Load application settings, then the generated tracing settings, in each service
terminal. Keep credential files out of terminal output and version control.

```sh
set -a
source .env
source .env.langfuse
set +a
```

`HELPDESK_TRACING_ENABLED=true` enables tracing. Otherwise all tracing wrappers
are no-ops. An explicit local Langfuse URL and keys are required; the application
never falls back to Langfuse Cloud. Only loopback UI/object-store ports are
published. Export is batched; service shutdown performs a bounded flush.
Export failures should not change a business result. A malformed enabled
configuration fails startup so a broken destination is not silently accepted.

## Separate application, evaluation, and protocol traces

Use the **Environment** filter in Langfuse Tracing:

| Environment | Content | Root observations |
| --- | --- | --- |
| `sdk-experiment` | Native model experiments, judge calibration, and explicitly labeled cached regrading | `experiment-item-run` |
| `protocol-tests` | Correctness tests using scripted models and direct MCP/mock operations | `eval.protocol_run`, with `protocol.test` children; `eval.run` holds report/pending-capability records |
| `development` | Ordinary application requests; also historical debug/test data recorded before separation | `helpdesk.request` for application requests |

The installed Langfuse SDK reserves `sdk-experiment` for its experiment trees.
Evaluation entry points use the same environment, so existing native experiments
remain accessible under that filter. Cached trajectories keep their explicit
execution-mode metadata and `cached-trajectories` tag; they do not represent new
agent calls. Ordinary service defaults can be configured with
`LANGFUSE_TRACING_ENVIRONMENT`; `development` remains the default.

`python -m helpdesk.evals --mode protocol` creates an `eval.protocol_run` parent
before pytest starts. Every test's setup, call, and teardown are grouped in one
`protocol.test`; the twenty requirement scores share that run's tree. Plain
pytest also labels its test roots `protocol-tests` when tracing is enabled.
SDK export-isolation tests retain their own in-memory exporters.

Test identifiers are bounded and accompanied by a SHA-256 digest. Raw pytest
parameter IDs can embed very large fixture values and must not become unbounded
trace metadata. Full local pytest reports remain separate from this display ID. Completed test
trees are flushed between tests to avoid overflowing the exporter queue during
fast correctness checks.

Across HTTP, only W3C `traceparent` and the `langfuse_environment` baggage field
are forwarded. The receiver validates the environment syntax and ignores other
baggage. This is a display label, never an identity, role, permission, or memory
source. User/session metadata and credentials are not forwarded as baggage. Explicit
application HTTP parents take precedence over native MCP transport metadata.
Nested application observations attach to the nearest exported application
ancestor, so filtered framework spans cannot leave missing parents.

Old records are preserved in their original environments. Selecting
`sdk-experiment` immediately excludes the historical standalone protocol/debug
roots shown in the unfiltered view. New protocol runs are explicitly separated;
no old traces are deleted or presented as newly executed model trials.

Sources: [Langfuse environments](https://langfuse.com/docs/observability/features/environments),
[Python propagation](https://langfuse.com/docs/observability/sdk/instrumentation),
and [pytest hook wrappers](https://docs.pytest.org/en/stable/how-to/writing_hook_functions.html).

## Privacy and operational limits

Inputs, outputs, arguments, and ticket contents can contain sensitive information.
This configuration is for the local synthetic assignment dataset. Credential
fields, configured secret values, and bearer tokens are redacted before export.
Headers are never recorded. Raw exception messages/stack locals and thinking
content are not captured by our observation wrappers. Reported reasoning token
usage is retained if supplied.

PII detection is explicitly deferred. Credential masking is not PII protection.
Do not assume arbitrary free-text sensitive data is sanitized. Before real user
data: add PII detection/redaction, content-capture controls, explicit trace access
policies, and a retention/deletion policy. Local volumes persist until removed;
automatic retention cleanup is not configured. Session grouping uses a hash of
both user and session, preventing shared session strings from merging users.

Tracing is instrumentation. Conversational approval and expiring user facts are
implemented in the agent service, and the offline judge is in `evaluation.py`.
No AWS deployment has been performed. Native graph pauses record
`awaiting_approval` rather than an operational error. Approval decisions,
write outcomes, same-ID reconciliation and synthetic fault modes are observable.

## Verification

Tests inspect actual SDK exports for tree correlation, effective model context,
usage omission/normalization, first output, errors, attempts/backoff, user-session
isolation, and credential masking. Local server ingestion is checked independently
through the Observations API; SDK flush alone does not prove read visibility.
Use the v5-compatible
[Observations API](https://langfuse.com/docs/api-and-data-platform/features/query-via-sdk).

Debug a bad run by finding its request ID, inspecting model context and tool
arguments, comparing returned data with the final answer, then checking attempts,
stop reasons, and the durable write/approval outcome. For deterministic eval
failures, follow the case ID and recorded failing assertion first.

## Model-first sequencing and evaluation experiments

`model.generate` precedes the first `tool.execute`, `mcp.discovery`, and ticket
RPC. There is no eager MCP connection or application-generated overview call.
The model first selects the local `discover_tools` utility, which initiates MCP
authentication, live membership checks, and schema discovery. Clarification or
out-of-scope answers may have no MCP observations. Application authentication
still verifies the caller before model work; ticket authorization remains in MCP
and the backend, independent of model decisions.
Model/tool observations are siblings in the agent loop; one model invocation
finishes before its requested tool executes. A deterministic exported-span test
checks this chronological ordering. The adapter may refresh output schemas
after later tool calls; these additional discovery spans also follow a model
decision and must not be mistaken for application-selected ticket retrieval.

The native `helpdesk/*/v5` datasets hold tool, quality, and adversarial cases.
Each SDK experiment creates item traces with code metrics and independent
`judge.*` dimensions, plus latency and available agent usage. `judge.answer` is
a logical span with rubric/evidence and decisions. Its `judge.attempt` generation
children use Opus 5.5 primarily and Opus 4.8 only after a provider refusal, with
per-model usage and no duplicate parent usage. Judge errors remain visible and
cannot pass joint evaluation. The main ReAct agent never receives this fallback.
Repeated trials are separate native experiments because each dataset item appears
once per experiment. Calibration anchors have their own dataset and run.

See [EVALUATION.md](EVALUATION.md) for exact metric definitions, the judge's
same-family bias, synthetic anchor limitations, and preserved failed experiments.
All evaluation provider calls run in the local process; the Langfuse containers
do not receive the Anthropic API key.


Native span and experiment identifiers must remain exact strings. The export
mask parses only structured JSON objects/arrays for nested redaction; it does
not normalize scalar attributes as JSON numbers. This avoids corrupting a
hexadecimal identifier that resembles scientific notation. Credential values
are still redacted in both structured payloads and scalar strings.


Verified separation: the latest protocol-only run passed 251 tests and all
20 requirement groups. Its 2,728 observations share the `protocol-tests`
environment, one `eval.protocol_run` root, and no missing parents. This includes
scripted generation observations, not paid model-quality trials. See
[evidence](evidence/trace-separation.json).


## Request workflow and browser tool summaries

Screening and agent execution are connected by the stateless outer LangGraph
workflow in `agent/workflow.py`. Only its accepted `agent` branch invokes the
SQLite-backed create_agent graph. Graph framework events are not exported as
additional roots; existing custom spans keep one request tree. Browser tool
summaries are fetched separately after completion and are not streamed into
chat text. The request UUID alone grants no access to another user's summary.

## Review correction telemetry

`approval.recover` records a tools-only replay from the same durable model-call
checkpoint. It does not produce another model generation. Normal execution and
explicit reconciliation share `mcp.transport_error`; unknown outcomes are saved
before approved dispatch and confirmed using `approval.outcome_resolved`.

`first_answer_token_ms` measures the first public answer fragment, not raw
provider TTFT. Final-only delivery buffers model fragments until final-message
validation, so this now includes final generation latency. Provider-first-token
metrics, where available, describe a different boundary. Historical pre-review
traces and their timing are unchanged.
