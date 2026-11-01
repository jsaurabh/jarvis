from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from .config import settings
from .export import export_dataset
from .flywheel import build_improvement_bundle
from .store import TraceStore


def evaluate(metrics: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if metrics["training_eligible"] < 25:
        reasons.append("At least 25 human-vetted examples are required before model promotion.")
    if metrics["rated"] < 10:
        reasons.append("At least 10 explicitly rated runs are required.")
    positive_rate = metrics.get("positive_rate")
    if positive_rate is None or positive_rate < 0.8:
        reasons.append("Positive rating rate must be at least 80%.")
    if metrics["failure_rate"] > 0.1:
        reasons.append("Run failure rate must be at most 10%.")
    return not reasons, reasons


def snapshot(store: TraceStore) -> dict[str, Any]:
    metrics = store.quality_metrics()
    passed, reasons = evaluate(metrics)
    snapshot_id = store.create_eval_snapshot(metrics, passed, " ".join(reasons))
    return {"snapshot_id": snapshot_id, "passed": passed, "metrics": metrics, "reasons": reasons}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate and gate the Jarvis improvement flywheel"
    )
    parser.add_argument(
        "action",
        choices=[
            "report",
            "snapshot",
            "export",
            "bundle",
            "register",
            "evaluate",
            "promote",
            "use-cloud",
        ],
    )
    parser.add_argument("--db", type=Path, default=settings.jarvis_db_path)
    parser.add_argument("--output", type=Path, default=Path("exports/jarvis-sft.jsonl"))
    parser.add_argument("--name")
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--base-model", default=settings.jarvis_model)
    parser.add_argument("--bundle-dir", type=Path, default=Path("exports/flywheel"))
    parser.add_argument("--registry", type=Path, default=settings.jarvis_model_registry)
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--baseline")
    args = parser.parse_args()
    store = TraceStore(args.db, "full")

    if args.action == "report":
        print(json.dumps(store.quality_metrics(), indent=2, sort_keys=True))
    elif args.action == "snapshot":
        print(json.dumps(snapshot(store), indent=2, sort_keys=True))
    elif args.action == "export":
        train, evaluation, eval_path = export_dataset(store, args.output, "trajectory", 0.2)
        print(
            json.dumps({"train": train, "eval": evaluation, "eval_path": str(eval_path)}, indent=2)
        )
    elif args.action == "bundle":
        print(
            json.dumps(build_improvement_bundle(store, args.bundle_dir), indent=2, sort_keys=True)
        )
    elif args.action in {"register", "evaluate", "promote", "use-cloud"}:
        from .model_lifecycle import (
            atomic_json,
            evaluate_candidate,
            promote,
            read_registry,
            register,
        )

        try:
            if args.action == "use-cloud":
                registry = read_registry(args.registry)
                registry["active"] = None
                atomic_json(args.registry, registry)
                print("Configured OpenAI model selected; restart Jarvis to apply.")
            else:
                if not args.name:
                    parser.error("--name is required")
                if args.action == "register":
                    if not args.artifact:
                        parser.error("register requires --artifact")
                    register(args.registry, args.name, args.artifact)
                    print(f"Registered candidate {args.name}; candidate evaluation is required.")
                elif args.action == "evaluate":
                    if not args.cases:
                        parser.error("evaluate requires --cases")
                    report = asyncio.run(
                        evaluate_candidate(args.registry, args.name, args.cases, args.baseline)
                    )
                    print(json.dumps(report, indent=2))
                    if not report["passed"]:
                        raise SystemExit(1)
                else:
                    promote(args.registry, args.name)
                    print(f"Promoted {args.name}; restart Jarvis to apply.")
        except (ValueError, KeyError, OSError) as exc:
            parser.error(str(exc))


if __name__ == "__main__":
    main()
