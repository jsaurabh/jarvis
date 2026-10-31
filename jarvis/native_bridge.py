from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from .agent import JarvisAgent
from .config import settings
from .store import TraceStore
from .tools import ToolExecutor

SESSION_FILE = Path("data/native-session-id")


def native_session(store: TraceStore) -> str:
    if SESSION_FILE.exists():
        session_id = SESSION_FILE.read_text(encoding="utf-8").strip()
        if store.get_session(session_id):
            return session_id
    session_id = store.create_session()
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(session_id, encoding="utf-8")
    return session_id


async def execute(args: argparse.Namespace) -> dict[str, Any]:
    store = TraceStore(settings.jarvis_db_path, settings.jarvis_trace_mode)
    agent = JarvisAgent(settings, store, ToolExecutor(settings))
    if args.action == "chat":
        return await agent.chat(native_session(store), args.text)
    if args.action == "approve":
        return await agent.resolve_approval(args.approval_id, args.approved == "true")
    if args.action == "feedback":
        store.feedback(args.run_id, args.rating, args.correction)
        return {"status": "completed", "message": "Feedback saved.", "run_id": args.run_id}
    if args.action == "trace-turn":
        metadata = json.loads(args.metadata)
        run_id = store.record_completed_turn(
            native_session(store),
            args.user_text,
            args.assistant_text,
            args.model,
            metadata,
        )
        return {"status": "completed", "message": "Voice turn saved.", "run_id": run_id}
    raise ValueError(f"Unknown action: {args.action}")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Native Jarvis bridge")
    subparsers = root.add_subparsers(dest="action", required=True)
    chat = subparsers.add_parser("chat")
    chat.add_argument("text")
    approval = subparsers.add_parser("approve")
    approval.add_argument("approval_id")
    approval.add_argument("approved", choices=["true", "false"])
    feedback = subparsers.add_parser("feedback")
    feedback.add_argument("run_id")
    feedback.add_argument("rating", type=int, choices=[-1, 1])
    feedback.add_argument("correction", nargs="?", default=None)
    trace_turn = subparsers.add_parser("trace-turn")
    trace_turn.add_argument("user_text")
    trace_turn.add_argument("assistant_text")
    trace_turn.add_argument("model")
    trace_turn.add_argument("metadata")
    return root


def main() -> None:
    args = parser().parse_args()
    try:
        result = asyncio.run(execute(args))
    except Exception as exc:
        result = {"status": "error", "message": str(exc)}
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
