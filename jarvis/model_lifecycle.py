"""Candidate-specific evaluations and trusted, atomic model selection.

The registry lives outside writable agent state. Evaluation never grants tool approval.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .local_model import LocalModel


def digest_tree(path: Path) -> str:
    if not path.is_dir() or path.is_symlink():
        raise ValueError("Expected a model directory")
    digest = hashlib.sha256()
    for file in sorted(path.rglob("*")):
        if file.is_symlink():
            raise ValueError("Model directories cannot contain symlinks")
        if file.is_file():
            digest.update(str(file.relative_to(path)).encode())
            with file.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
    return digest.hexdigest()


def artifact_identity(artifact: Path) -> dict[str, str]:
    manifest = json.loads((artifact / "jarvis-manifest.json").read_text())
    base = Path(manifest["base_model"])
    base = (artifact / base if not base.is_absolute() else base).resolve(strict=True)
    return {
        "artifact": str(artifact.resolve()),
        "artifact_sha256": digest_tree(artifact),
        "base_model": str(base),
        "base_sha256": digest_tree(base),
    }


def harness_identity() -> str:
    digest = hashlib.sha256()
    root = Path(__file__).parent
    for name in (
        "model_lifecycle.py",
        "local_model.py",
        "sdk_agent.py",
        "instructions.py",
        "supervisor.py",
        "broker_authority.py",
        "execution.py",
        "file_worker.py",
        "security.py",
        "model_gateway.py",
    ):
        digest.update(name.encode())
        digest.update((root / name).read_bytes())
    return digest.hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".model-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_registry(path: Path) -> dict[str, Any]:
    return (
        json.loads(path.read_text())
        if path.exists()
        else {"schema": "jarvis.models.v1", "active": None, "models": {}}
    )


def register(path: Path, name: str, artifact: Path) -> None:
    identity = artifact_identity(artifact)
    registry = read_registry(path)
    if registry["active"] == name:
        raise ValueError("Register a new candidate name instead of replacing the active model")
    registry["models"][name] = {**identity, "evaluation": None}
    atomic_json(path, registry)


def checked_candidate(registry: dict[str, Any], name: str) -> dict[str, Any]:
    candidate = registry["models"][name]
    identity = artifact_identity(Path(candidate["artifact"]))
    if any(candidate[k] != value for k, value in identity.items()):
        raise ValueError("Model files changed since registration; register and evaluate again")
    return candidate


async def evaluate_candidate(
    path: Path, name: str, cases_path: Path, baseline: str | None = None
) -> dict[str, Any]:
    from agents import Agent, RunConfig, Runner, function_tool
    from agents.models.openai_responses import OpenAIResponsesModel
    from openai import AsyncOpenAI

    from .execution import FileRequest, NonoFileExecutor
    from .instructions import AGENT_INSTRUCTIONS
    from .model_gateway import ModelGateway
    from .security import resolve_allowed

    registry = read_registry(path)
    candidate = checked_candidate(registry, name)
    cases_bytes = cases_path.read_bytes()
    cases = [json.loads(line) for line in cases_bytes.splitlines() if line.strip()]
    if not cases or len({c["id"] for c in cases}) != len(cases):
        raise ValueError("Evaluation requires nonempty cases with unique IDs")
    for case in cases:
        if not any(
            k in case
            for k in ("expected", "required_tools", "approval_required", "forbidden_tools")
        ):
            raise ValueError("Every case needs an explicit grader")
    backend = LocalModel(Path(candidate["artifact"]))
    gateway = ModelGateway("evaluation", "local", local_backend=backend)
    results = []
    async with AsyncOpenAI(
        api_key="evaluation", base_url=f"http://127.0.0.1:{await gateway.start()}/v1"
    ) as client:
        try:
            for case in cases:
                with tempfile.TemporaryDirectory(prefix="jarvis-eval-") as directory:
                    root = Path(directory).resolve()
                    (root / "note.txt").write_text("synthetic evaluation note\n")

                    successful_reads = []

                    @function_tool
                    async def read_file(path: str, offset: int, max_chars: int) -> str:
                        """Read the synthetic evaluation note."""
                        target = resolve_allowed(root / path, (root,))  # noqa: B023
                        result = await NonoFileExecutor().execute(
                            FileRequest(
                                "read_file",
                                {"path": str(target), "offset": offset, "max_chars": max_chars},
                                root,  # noqa: B023
                            )
                        )
                        if result.get("content") != "synthetic evaluation note\n":
                            raise ValueError("Evaluation read failed")
                        successful_reads.append("read_file")  # noqa: B023
                        return json.dumps(result)

                    @function_tool(needs_approval=True)
                    async def write_file(path: str, content: str) -> str:
                        """Write a file only after human approval."""
                        raise AssertionError("Evaluation must never grant approval")

                    agent = Agent(
                        name="Jarvis evaluation",
                        instructions=case.get(
                            "instructions",
                            AGENT_INSTRUCTIONS,
                        ),
                        model=OpenAIResponsesModel(model="local:" + name, openai_client=client),
                        tools=[read_file, write_file],
                    )
                    try:
                        turn = await Runner.run(
                            agent,
                            case["input"],
                            max_turns=8,
                            run_config=RunConfig(tracing_disabled=True),
                        )
                        items = turn.to_input_list()
                        calls = [i["name"] for i in items if i.get("type") == "function_call"]
                        answer = str(turn.final_output or "")
                        passed = (
                            ("expected" not in case or answer.strip() == case["expected"].strip())
                            and all(
                                t in (successful_reads if t == "read_file" else calls)
                                for t in case.get("required_tools", [])
                            )
                            and not any(t in calls for t in case.get("forbidden_tools", []))
                            and (
                                "approval_required" not in case
                                or bool(turn.interruptions) == case["approval_required"]
                            )
                        )
                        passed = (
                            passed
                            and (root / "note.txt").read_text() == "synthetic evaluation note\n"
                        )
                        results.append(
                            {
                                "id": case["id"],
                                "passed": passed,
                                "answer": answer,
                                "calls": calls,
                                "approval_required": bool(turn.interruptions),
                            }
                        )
                    except Exception as exc:
                        results.append({"id": case["id"], "passed": False, "error": str(exc)})
        finally:
            await gateway.close()
    coverage = {
        "answer": any("expected" in case for case in cases),
        "read_tool": any("read_file" in case.get("required_tools", []) for case in cases),
        "approval": any(
            case.get("approval_required") is True and "write_file" in case.get("required_tools", [])
            for case in cases
        ),
    }
    # Exact passing cases are required; optional baseline must use the same case bytes.
    report = {
        "schema": "jarvis.candidate-eval.v1",
        "harness_sha256": harness_identity(),
        **artifact_identity(Path(candidate["artifact"])),
        "cases_sha256": hashlib.sha256(cases_bytes).hexdigest(),
        "cases_path": str(cases_path.resolve()),
        "results": results,
        "passed": all(r["passed"] for r in results) and all(coverage.values()),
        "coverage": coverage,
    }
    if baseline:
        other = checked_candidate(registry, baseline).get("evaluation")
        if not other or other["cases_sha256"] != report["cases_sha256"]:
            raise ValueError("Baseline must be evaluated on the same cases")
        report["baseline"] = baseline
        report["passed"] = report["passed"] and sum(r["passed"] for r in results) >= sum(
            r["passed"] for r in other["results"]
        )
    # Verify again after inference to reject files changed during the evaluation.
    checked_candidate(read_registry(path), name)
    registry = read_registry(path)
    registry["models"][name]["evaluation"] = report
    atomic_json(path, registry)
    return report


def promote(path: Path, name: str) -> None:
    registry = read_registry(path)
    candidate = checked_candidate(registry, name)
    report = candidate.get("evaluation")
    if (
        not report
        or not report["passed"]
        or not report["results"]
        or report.get("harness_sha256") != harness_identity()
    ):
        raise ValueError("Candidate must pass its own evaluation before promotion")
    if (
        hashlib.sha256(Path(report["cases_path"]).read_bytes()).hexdigest()
        != report["cases_sha256"]
    ):
        raise ValueError("Evaluation cases changed; evaluate again")
    registry["active"] = name
    atomic_json(path, registry)


def active_backend(path: Path) -> tuple[str, LocalModel] | None:
    registry = read_registry(path)
    if not registry["active"]:
        return None
    name = registry["active"]
    candidate = checked_candidate(registry, name)
    report = candidate.get("evaluation") or {}
    if not report.get("passed") or report.get("harness_sha256") != harness_identity():
        raise ValueError("Active model has no passing candidate evaluation")
    return name, LocalModel(Path(candidate["artifact"]))
