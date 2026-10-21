# Latest measured evaluation

Run date: 2026-10-20. See [the JSON report](latest-all.json) for the exact UTC
timestamp, run ID, hashes, named cases, and per-test evidence.

| Layer | Actual result |
| --- | --- |
| Correctness tests | 232 passed, zero failures |
| Protocol case groups | 20 passed |
| Real-model tasks | 23 passed, one failed out of 24 Sonnet 5 tasks; one trial each |
| Pending capability cases | Nine blocked: chat approval, cross-session memory, browser flow, runtime mock failure switch |
| Full-run exit status | 1: M24 fails the English-only response policy |
| Provider generation | 45 logical calls in the full suite, plus two in the preliminary smoke case; actual SDK HTTP retries are not measured |

The run exported code-based evaluator observations and scores to local Langfuse.
Server ingestion was verified independently through its API; see
[Langfuse verification evidence](../../docs/evidence/langfuse-verification.json).
Protocol and eval-harness checks use controlled model responses. The separate
24-task layer used the real configured `claude-sonnet-5` through Anthropic.
Settings: adaptive thinking, medium effort, max_tokens 1024, timeout 30 seconds,
two SDK retries, one independent trial per task.

Provider-reported usage in the full model suite: 138,315 input tokens and 5,894
output tokens (144,209 total). No price or unobserved retry count was inferred.
See [the compact model summary](real-model-summary.json) and
[the preliminary smoke trial](model-smoke.json).

## Observed regression and review notes

M24 asks for a Russian answer while the system requires English. The model
correctly read the requested ticket and reported the right status, but answered
in Russian. The `english_only` check failed. The original result is retained;
the prompt and expected result were not changed to make this measurement pass.

AI-assisted transcript inspection also found issues not captured by the binary
graders: M10 adds an unnecessary claim about additional access restrictions;
M18 offers another lookup "this turn" after retries are exhausted; M19 uses
"open one" ambiguously in a read-only stage. Backend enforcement still prevents
unauthorized reads and writes. These notes are not calibrated human grades.
Several replies are longer than the requested concise style.

The observed automated task pass rate is 23/24 for this single trial, not a
production reliability estimate. Subjective refusal, ambiguity, and explanation
quality still need user review. Nine pending capabilities remain unimplemented.

Non-fatal warnings: the MCP integration remains beta; programmatic pytest startup
reports already-imported plugin assertion-rewrite warnings. One OpenTelemetry
status-description type warning appeared during the real run; model cases and
score persistence completed. It remains a tracing diagnostic to investigate.
No failed task was suppressed. There is no claimed full assignment pass.
