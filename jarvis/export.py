from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .config import settings
from .learning import (
    agent_sft_row,
    preference_row,
    sensitive_reasons,
    split_group,
    trajectory_v2,
    utc_now,
)
from .store import TraceStore


def selected_runs(store: TraceStore) -> list[dict[str, Any]]:
    """Only explicit positive feedback or a human correction is training-eligible."""
    return store.exportable_runs()


def to_sft(run: dict[str, Any]) -> dict[str, Any]:
    answer = run.get("correction") or run.get("assistant_text") or ""
    return {
        "messages": [
            {"role": "user", "content": run["user_text"]},
            {"role": "assistant", "content": answer},
        ],
        "metadata": {
            "run_id": run["id"],
            "model": run["model"],
            "source": "human_corrected" if run.get("correction") else "human_approved",
        },
    }


def to_chatml(run: dict[str, Any]) -> dict[str, Any]:
    answer = run.get("correction") or run.get("assistant_text") or ""
    return {
        "conversations": [
            {"from": "human", "value": run["user_text"]},
            {"from": "gpt", "value": answer},
        ],
        "id": run["id"],
    }


def to_trajectory(store: TraceStore, run: dict[str, Any]) -> dict[str, Any]:
    return trajectory_v2(store, run)


def is_holdout(run_id: str, fraction: float) -> bool:
    bucket = int(hashlib.sha256(run_id.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return bucket < fraction


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            count += 1
    return count


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def export_dataset(
    store: TraceStore,
    output: Path,
    format_name: str,
    holdout: float,
    include_sensitive: bool = False,
) -> tuple[int, int, Path]:
    runs = selected_runs(store)
    converter = {
        "sft": to_sft,
        "chatml": to_chatml,
        "trajectory": lambda run: to_trajectory(store, run),
        "agent_sft": lambda run: agent_sft_row(store, run),
        "preference": lambda run: preference_row(store, run),
    }[format_name]
    rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    excluded_sensitive: dict[str, list[str]] = {}
    incompatible = 0
    for run in runs:
        row = converter(run)
        if row is None:
            incompatible += 1
            continue
        reasons = sensitive_reasons(row)
        if reasons and not include_sensitive:
            excluded_sensitive[run["id"]] = reasons
            continue
        rows.append((run, row))
    training = [row for run, row in rows if not is_holdout(split_group(store, run), holdout)]
    evaluation = [row for run, row in rows if is_holdout(split_group(store, run), holdout)]
    train_count = write_jsonl(output, training)
    eval_path = output.with_name(output.stem + ".eval" + output.suffix)
    eval_count = write_jsonl(eval_path, evaluation)
    manifest = {
        "schema": "jarvis.dataset-manifest.v1",
        "created_at": utc_now(),
        "format": format_name,
        "source_db": str(store.path.resolve()),
        "selection": "completed runs with explicit positive feedback or a human correction",
        "split_unit": "task, otherwise session-day",
        "holdout_fraction": holdout,
        "train_examples": train_count,
        "eval_examples": eval_count,
        "excluded_sensitive": excluded_sensitive,
        "incompatible_examples": incompatible,
        "include_sensitive": include_sensitive,
        "train_sha256": file_sha256(output),
        "eval_sha256": file_sha256(eval_path),
    }
    manifest_path = output.with_name(output.stem + ".manifest.json")
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return train_count, eval_count, eval_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Export human-vetted Jarvis traces")
    parser.add_argument("--db", type=Path, default=settings.jarvis_db_path)
    parser.add_argument("--output", type=Path, default=Path("exports/jarvis-sft.jsonl"))
    parser.add_argument(
        "--format",
        choices=["sft", "chatml", "trajectory", "agent_sft", "preference"],
        default="agent_sft",
    )
    parser.add_argument("--holdout", type=float, default=0.2)
    parser.add_argument(
        "--include-sensitive",
        action="store_true",
        help="Include examples containing personal-data patterns; off by default.",
    )
    args = parser.parse_args()
    if not 0 <= args.holdout < 1:
        parser.error("--holdout must be between 0 (inclusive) and 1 (exclusive)")
    store = TraceStore(args.db, "full")
    train_count, eval_count, eval_path = export_dataset(
        store, args.output, args.format, args.holdout, args.include_sensitive
    )
    print(f"Wrote {train_count} training examples to {args.output}")
    print(f"Wrote {eval_count} holdout examples to {eval_path}")


if __name__ == "__main__":
    main()
