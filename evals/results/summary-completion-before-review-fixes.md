# Completion evaluation — 2026-10-21

**Final completion contract: 17/17 code checks and 17/17 Opus 5.5 judgments passed.**
All 30 expected input-screening turns were classified correctly, with no observed
false blocks or false allows. This is one trial per inspected synthetic case,
not a production reliability estimate or an independent security benchmark.

| Suite | Cases | Joint passes | Langfuse experiment |
| --- | ---: | ---: | --- |
| Tool/workflow | 11 | 11 | [Open](http://127.0.0.1:3000/project/helpdesk-assignment/experiments/results?baseline=0e4ce25a5fb2421e) |
| Answer/memory | 2 | 2 | [Open](http://127.0.0.1:3000/project/helpdesk-assignment/experiments/results?baseline=6afbbc9b3fa633a2) |
| Adversarial/control | 4 | 4 | [Open](http://127.0.0.1:3000/project/helpdesk-assignment/experiments/results?baseline=b28ae52abaecdb87) |

## Scope and measurements

The frozen completion dataset v1 has its own `helpdesk-completion` namespace.
It covers create/comment proposals, next-message approval, decline, duplicates,
restart, uncertain committed writes, failed writes and explicit retry, runtime
429/slow responses, user facts and isolation, unauthorized targets, blocked
requests and malicious tool content. Assertions use actual database deltas and
application approval records. The judge receives those facts and actual tool
evidence; it cannot authorize a write.

- Correctness: **326 Python tests passed**, **29/29 protocol requirements passed**,
  and no current pending feature checks. [Protocol report](latest-protocol.json).
- Frontend: **11/11** stream-parser and review-request tests passed.
- Judge dimensions: groundedness, helpfulness, scope adherence, instruction
  adherence and clarity each **17/17**. Three explicitly attack-labeled trials
  had no observed attack success; the fourth adversarial-suite item is a control.
- Agent-trial latency: p50 **9.20 s**, p95 **16.92 s**, excluding offline judging.
- Actual browser buttons restored two proposals: approve added one comment;
  decline added none. This uses a scripted model with the real graph/MCP/API.
- Final Docker image built; mock, MCP and chat ran healthy. Real Sonnet proposed
  a comment without mutation; after restarting mock/chat, two approvals added
  exactly one comment. The authenticated runtime rate-limit switch was also
  exercised through the real container network. See [completion evidence](../../docs/COMPLETION.md).

## Configuration

Agent: `claude-sonnet-5-5`, adaptive thinking, medium effort, 1024 output tokens.
Guardrail: Sonnet 5.5, tool-free `between_tools`, low effort, one plain digit.
Judge: `claude-opus-5-5`, adaptive thinking, medium effort, 2048 output tokens.
Only guardrail/judge refusals allow one Opus 4.8 reserve. Provider calls have a
30-second timeout and two SDK retries; requests have a 60-second deadline.
The backend operation deadline is ten seconds; the MCP transport allows twelve.
Model-selected write proposals stop through native LangGraph interrupt. Validated
application receipts handle completion, decline and write errors without an
extra post-approval model call.

## Preserved failures and repeats

The first [17-case run](experiment-35b600020c9942549a5eb265aeeac277.json)
passed **16/17 jointly**. C13 exposed a real timeout race: backend and MCP both
expired at ten seconds, so the transport exception escaped the typed tool-error
path. The backend deadline stays ten seconds, MCP now permits twelve, and safe
SDK error mapping handles transport expiry independently. The corrected
[C13 repeat](experiment-b4ad492dd8d24036bdc53a4736856402.json) passed **2/2**;
the final full run separately passed 17/17. No failed report was replaced and no
case expectation was relaxed. The earlier
[four-case smoke](experiment-96a30c5f956848fb91f02e0c38d217dd.json) passed 4/4.
An initial protocol failure caused by ambient test credentials is preserved in
[its original report](protocol-completion-initial.json); unit fixtures now
explicitly opt out of the environment's production approval bridge.

The [final report](experiment-c0ef4c5e9a1d4728ae70dba3a6840914.json) records source,
prompt, dataset and lock hashes. [Langfuse API read-back](../../docs/evidence/completion-langfuse.json)
confirmed all 17 native items and passing joint scores, and matching current
application/agent/guardrail/dataset/lock hashes. Observation names and write-trace
parentage were read back; complete per-dimension export, billed cost and physical
provider retry counts are not asserted.

The [previous Sonnet 5.5 migration summary](summary-sonnet-5-5-before-completion.md)
retains its 53/54 read-only result and unchanged repeats. Original v5 labels are
unchanged and were not redefined as write-flow passes. The new 17-case contract
is a different dataset, not a claimed improvement from 53/54.

## Reproduction and limits

```sh
uv run python -m helpdesk.evals --mode protocol
node --test frontend/tests/*.test.mjs
uv run --env-file .env --env-file .env.langfuse python -m helpdesk.evaluation --dataset evals/completion-datasets.json
```

No lint or type checker is configured. AI-assisted inspected labels, a
same-family LLM judge, limited fixtures and one final trial do not prove broad
generalization. Independent human review, load/security testing, production
authentication and history/trace cleanup remain future work. PII detection is
explicitly deferred. The AWS design and candidate's personal reviewed writeup
remain submission work; this batch is not a completed AWS deployment.
