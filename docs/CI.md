# Continuous integration

`.github/workflows/ci.yml` runs on pull requests, pushes to `main`, and manual
workflow dispatch. Three independent Ubuntu 24.04 jobs provide separate checks:

| Check | Command | Scope |
| --- | --- | --- |
| Python and protocol checks | `uv sync --locked`, then `uv run --locked python -m helpdesk.evals --mode protocol` | All Python correctness tests and requirement mappings, including R01–R04 |
| JavaScript checks | `node --test frontend/tests/*.test.mjs` | SSE parser and exact approve/decline request binding |
| Docker build | `docker build --progress=plain --tag helpdesk-ci:test .` | Locked application image and packaged resources |

Python 3.13 and uv 0.9.5 match the application image. Node.js 22 matches the
locally verified frontend tests. Dependency installation must accept `uv.lock`
unchanged; CI does not resolve or update dependencies. uv caches are keyed by
the lockfile. No additional test framework or runtime dependency is introduced.

## Results and credentials

No repository secrets are required. Tracing is disabled and protocol tests use
synthetic model responses with real graph/MCP/mock boundaries. Anthropic and
Langfuse services are not contacted. Provider behavior and LLM-judge experiments
remain separately invoked, paid evaluations; CI does not run them.

Each job uploads its report/log as a seven-day artifact, including on failure.
Protocol execution removes the checkout's historical report before running and
copies only its newly generated report into the artifact directory. A missing
report is not presented as a passing historical result. Bash pipefail preserves
test/build failure through `tee`; artifact upload does not suppress a failed job.
Setup failures can leave no report, in which case upload reports a warning.

The workflow grants only `contents: read`, disables checkout credential
persistence, and pins all Actions to verified commit SHAs. New runs cancel an
older run of the same workflow/ref. There is no deployment, image publishing,
production credential access or automatic merge. Build logs use the secret-free
Docker context; `.env` files and runtime data are excluded by `.dockerignore`.

## Local checks

```sh
uv sync --locked
HELPDESK_TRACING_ENABLED=false uv run --locked python -m helpdesk.evals --mode protocol
node --test frontend/tests/*.test.mjs
docker build --progress=plain --tag helpdesk-ci:test .
```

The protocol command updates `evals/results/latest-protocol.json` locally.
Preserve any historical report that must remain as evidence before running it.
The workflow does not commit generated reports back to the repository.

The file takes effect after it is pushed to GitHub. A local validation is not a
GitHub-hosted run. Branch protection is a separate repository setting: after a
successful first run, the three named checks can be required before merging.
This workflow does not change that setting. The Docker job builds the image;
it does not claim container startup or provider end-to-end validation.

## Initial local validation — 2026-10-22

`actionlint` 1.7.12 accepted the workflow. Its exact shell commands ran in an
isolated macOS checkout without local credentials: locked installation passed,
337 Python tests and 33 protocol mappings passed, 11 JavaScript tests passed,
and the Docker image built successfully. The original working checkout's
historical report was preserved. A controlled command failure kept exit code 7,
retained its log and omitted the stale report.

[Validation evidence](evidence/ci-validation.json) and the
[isolated protocol report](evidence/ci-protocol-local.json) record those results.
Those checks were local validation; the first hosted run below exposed a
separate SQLite scheduling defect.

## Updating Actions

Verify an upstream release/tag and its full commit SHA before replacing a pin.
Keep the version comments aligned with those commits. Validate changes with
`actionlint` and run the affected commands. Useful official references:

- [GitHub workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)
- [Concurrency](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)
- [Artifacts](https://docs.github.com/en/actions/tutorials/store-and-share-data)
- [setup-uv](https://github.com/astral-sh/setup-uv)

## First hosted run and SQLite correction

The [first hosted run](https://github.com/korolyow2/it-helpdesk-agent/actions/runs/39752631158)
on commit `a29d9fb` passed Docker/JavaScript, but Python passed 335/337 tests.
Its [original report](../evals/results/protocol-ci-first-run-failed.json) is retained.
Synchronous application-state SQLite calls could block the event loop while
an asynchronous checkpoint transaction needed that loop to finish committing.
The result was a busy timeout and OperationalError during proposal persistence.

Asynchronous service, approval middleware and memory endpoints now offload
those synchronous operations using `asyncio.to_thread`. Connections still open
and close within the worker that uses them; schema, transaction boundaries,
authorization, idempotency and timeout limits are unchanged. A deterministic
regression holds an actual SQLite writer and requires an event-loop coroutine
to release it. It failed before the correction and passes afterward.

The corrected local protocol run passed **338 tests and all 33 requirements**.
The [current report](../evals/results/latest-protocol.json) records its source
hash. Subsequent GitHub runs validate the correction on hosted Ubuntu; the
initial failed run remains separate evidence. No retry or relaxed assertion
was added to hide the failure.
