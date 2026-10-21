"""Regrade saved real trajectories after explicit dataset annotation corrections."""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from langfuse import propagate_attributes

from helpdesk.evals import grade
from helpdesk.evaluation import (
    DATASET, JUDGE_PROMPT, ROOT, judge_answer, judge_from_env,
    scores_for_result, summarize, tool_metrics, upload_dataset,
)
from helpdesk.telemetry import redact, tracing_client


def run_experiments():
    """Reuse agent trajectories, optionally calling a revised judge."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--rejudge", action="store_true")
    args = parser.parse_args()
    args.source = args.source.resolve()
    source = json.loads(args.source.read_text())
    catalog = json.loads(DATASET.read_text())
    cases = {case["id"]: case for case in catalog["cases"]}
    client = tracing_client()
    if client is None:
        parser.error("Configure local Langfuse before regrading.")
    judge = judge_from_env() if args.rejudge else None
    run_id = uuid4().hex
    report = {key: value for key, value in source.items() if key not in {"run_id", "summary", "results", "experiments"}}
    report.update(run_id=run_id, created_at=datetime.now(timezone.utc).isoformat(),
                  execution_mode="cached_trajectory_rejudge" if args.rejudge else "cached_trajectory_regrade", source_run_id=source["run_id"],
                  source_report=str(args.source.relative_to(ROOT)), new_agent_calls=0, new_judge_calls=len(source["results"]) if args.rejudge else 0,
                  source_judge_sha256=source["judge_sha256"],
                  judge_sha256=hashlib.sha256(JUDGE_PROMPT.encode()).hexdigest() if args.rejudge else source["judge_sha256"],
                  source_dataset_sha256=source["dataset_sha256"],
                  dataset_sha256=hashlib.sha256(DATASET.read_bytes()).hexdigest(),
                  dataset_version=catalog["version"],
                  annotation_corrections=[] if source["dataset_version"] == catalog["version"] else
                      ["M14 requires no tools for credential-disclosure refusal; inspected cases relabeled regression"],
                  results=[], experiments=[])
    for suite in dict.fromkeys(result["suite"] for result in source["results"]):
        dataset = upload_dataset(client, suite, [c for c in catalog["cases"] if c["suite"] == suite], catalog["version"])
        trials = sorted({r["trial"] for r in source["results"] if r["suite"] == suite})
        for trial in trials:
            originals = {r["id"]: r for r in source["results"] if r["suite"] == suite and r["trial"] == trial}
            dataset.items = [item for item in dataset.items if item.input["id"] in originals]

            async def task(*, item, **kwargs):
                """Reuse actual agent replies and optionally obtain new judge decisions."""
                result = deepcopy(originals[item.input["id"]])
                case = cases[result["id"]]
                result["split"] = case["split"]
                result["checks"] = grade(case, result["output"])
                result["tool_metrics"] = tool_metrics(case, result["output"])
                result["checks"].update(
                    reference_call_coverage=result["tool_metrics"]["reference_call_recall"] == 1,
                    appropriate_tools=result["tool_metrics"]["tool_precision"] == 1,
                    model_orchestrated=result["tool_metrics"]["model_orchestrated"] == 1,
                )
                result["status"] = "passed" if all(result["checks"].values()) else "failed"
                if args.rejudge:
                    result["judge"] = await judge_answer(case, result["output"], judge)
                report["results"].append(result)
                print(json.dumps({"case": result["id"], "trial": trial,
                                  "rejudge": args.rejudge, "code": result["status"],
                                  "judge_available": result["judge"].get("status") == "completed"}), flush=True)
                return result

            def evaluator(*, output, **kwargs):
                """Store corrected code scores alongside unchanged judge scores."""
                return scores_for_result(output)

            experiment = dataset.run_experiment(
                name="Helpdesk corrected annotation", run_name=f"{run_id}-{suite}-trial-{trial}",
                task=task, evaluators=[evaluator], max_concurrency=1,
                metadata={"execution_mode": report["execution_mode"], "source_run_id": source["run_id"],
                          "new_agent_calls": 0, "new_judge_calls": report["new_judge_calls"],
                          "prompt_sha256": source["prompt_sha256"], "judge_sha256": report["judge_sha256"]},
            )
            report["experiments"].append({"suite": suite, "trial": trial, "url": experiment.dataset_run_url,
                                          "experiment_id": experiment.experiment_id, "items": len(experiment.item_results)})
    report["summary"] = summarize(report["results"])
    destination = ROOT / "evals/results" / f"regrade-{run_id}.json"
    destination.write_text(json.dumps(redact(report), indent=2) + "\n")
    client.flush()
    print(json.dumps({"report": str(destination), "summary": report["summary"]}))


def main():
    """Keep evaluation observations separate from application and pytest traces."""
    with propagate_attributes(environment="sdk-experiment", tags=["cached-trajectories"]):
        run_experiments()


if __name__ == "__main__":
    main()
