from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .learning import sensitive_reasons, trajectory_v2, utc_now


def build_improvement_bundle(store: Any, output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    runs = store.all_runs()
    useful = [run for run in runs if run.get("rating") == 1 or run.get("correction")]
    failures = [
        run
        for run in runs
        if run.get("status") in {"failed", "cancelled"} or run.get("rating") == -1
    ]
    selected = []
    excluded_sensitive = 0
    # A negatively rated failed run can appear in both sets. Keep one episode
    # per run so metrics and review queues remain stable.
    candidates = {run["id"]: run for run in useful + failures}
    for run in candidates.values():
        episode = trajectory_v2(store, run)
        if sensitive_reasons(episode):
            excluded_sensitive += 1
            continue
        selected.append(episode)

    traces_path = output_dir / "reviewable-traces.jsonl"
    with traces_path.open("w", encoding="utf-8") as handle:
        for episode in selected:
            handle.write(json.dumps(episode, ensure_ascii=False, separators=(",", ":")) + "\n")

    eval_cases = []
    for run in runs:
        if not run.get("correction"):
            continue
        eval_cases.append(
            {
                "id": f"correction-{run['id']}",
                "input": run["user_text"],
                "expected": run["correction"],
                "source_run_id": run["id"],
                "grader": "semantic_and_tool_policy",
            }
        )
    eval_path = output_dir / "regression-cases.jsonl"
    with eval_path.open("w", encoding="utf-8") as handle:
        for case in eval_cases:
            handle.write(json.dumps(case, ensure_ascii=False, separators=(",", ":")) + "\n")

    contexts = store.distinct_harness_contexts()
    harness_path = output_dir / "harness-snapshots.json"
    harness_path.write_text(
        json.dumps(contexts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    metrics = store.quality_metrics()
    summary = {
        "schema": "jarvis.flywheel-bundle.v1",
        "created_at": utc_now(),
        "metrics": metrics,
        "reviewable_traces": len(selected),
        "regression_cases": len(eval_cases),
        "harness_versions": len(contexts),
        "excluded_sensitive": excluded_sensitive,
        "files": {
            "traces": str(traces_path),
            "eval_cases": str(eval_path),
            "harness_snapshots": str(harness_path),
        },
    }
    (output_dir / "bundle.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    report = f"""# Jarvis improvement handoff

Generated: {summary["created_at"]}

## Evidence

- Total runs: {metrics["total"]}
- Human-vetted training examples: {metrics["training_eligible"]}
- Corrected examples: {metrics["corrected"]}
- Failed runs: {metrics["failed"]}
- Reviewable traces in this bundle: {len(selected)}
- Durable regression cases: {len(eval_cases)}
- Harness snapshots: {len(contexts)}
- Sensitive traces excluded: {excluded_sensitive}

## Review loop

1. Inspect `reviewable-traces.jsonl` for recurring tool, routing, prompt, and safety failures.
2. Preserve or refine `regression-cases.jsonl` before changing the harness or model.
3. Compare the current agent and candidate on the same cases in a sandbox.
4. Promote only if task success improves without approval or safety regressions.
"""
    (output_dir / "codex-handoff.md").write_text(report, encoding="utf-8")
    return summary
