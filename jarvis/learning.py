from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from datetime import datetime
from typing import Any

TRACE_SCHEMA = "jarvis.trajectory.v2"
AGENT_SFT_SCHEMA = "jarvis.agent-sft.v1"
PREFERENCE_SCHEMA = "jarvis.preference.v1"
HARNESS_SCHEMA = "jarvis.harness.v1"
HARNESS_VERSION = "1.1.0"


_SENSITIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)),
    ("phone", re.compile(r"(?<!\d)(?:\+?1[ .-]?)?(?:\(?\d{3}\)?[ .-]?)\d{3}[ .-]?\d{4}(?!\d)")),
    ("ssn", re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")),
    ("api_key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("credential", re.compile(r"(?i)\b(?:password|passwd|api[_ -]?key|secret)\s*[:=]\s*\S+")),
)

_NON_CONTENT_KEYS = {
    "id",
    "created_at",
    "completed_at",
    "timestamp",
}


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def harness_snapshot(
    *,
    interface: str,
    model: str,
    instructions: str,
    tools: list[dict[str, Any]],
    model_settings: dict[str, Any] | None = None,
    skills: list[dict[str, Any]] | None = None,
    task_id: str | None = None,
    version: str = HARNESS_VERSION,
) -> dict[str, Any]:
    tool_specs = tools or []
    skill_specs = skills or []
    payload = {
        "schema": HARNESS_SCHEMA,
        "version": version,
        "interface": interface,
        "model": model,
        "instructions": instructions,
        "tools": tool_specs,
        "model_settings": model_settings or {},
        "skills": skill_specs,
        "task_id": task_id,
    }
    payload["instructions_sha256"] = fingerprint(instructions)
    payload["tools_sha256"] = fingerprint(tool_specs)
    payload["skills_sha256"] = fingerprint(skill_specs)
    payload["harness_sha256"] = fingerprint(payload)
    return payload


def _privacy_text(value: Any, key: str | None = None) -> Iterable[str]:
    """Yield user-observable content without scanning IDs, hashes, or timestamps."""
    if key and (
        key in _NON_CONTENT_KEYS
        or key.endswith("_id")
        or key.endswith("_sha256")
        or key.endswith("_at")
    ):
        return
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            yield from _privacy_text(child_value, str(child_key))
    elif isinstance(value, (list, tuple, set)):
        for child in value:
            yield from _privacy_text(child)
    elif isinstance(value, str):
        yield value
    elif isinstance(value, (int, float)):
        yield str(value)


def sensitive_reasons(value: Any) -> list[str]:
    text = "\n".join(_privacy_text(value))
    reasons = {name for name, pattern in _SENSITIVE_PATTERNS if pattern.search(text)}
    return sorted(reasons)


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("transcript") or item.get("content")
                if text is not None:
                    parts.append(_content_text(text))
        return "".join(parts)
    if isinstance(value, dict):
        return _content_text(value.get("text") or value.get("content") or "")
    return "" if value is None else str(value)


def _arguments(value: Any) -> str:
    if isinstance(value, str):
        return value
    return stable_json(value or {})


def _tool_output(value: Any) -> str:
    if isinstance(value, str):
        return value
    return stable_json(value)


def _append_item(messages: list[dict[str, Any]], item: dict[str, Any]) -> None:
    item_type = item.get("type")
    role = item.get("role")
    # Hidden reasoning is never training data. Some otherwise-useful response
    # items can also carry an encrypted reasoning field, so discard that field
    # rather than dropping the observable message or tool call with it.
    if item_type == "reasoning":
        return
    item = {key: value for key, value in item.items() if key != "encrypted_content"}
    if item_type == "function_call":
        messages.append(
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": str(item.get("call_id") or item.get("id") or ""),
                        "type": "function",
                        "function": {
                            "name": str(item.get("name") or "tool"),
                            "arguments": _arguments(item.get("arguments")),
                        },
                    }
                ],
            }
        )
        return
    if item_type == "function_call_output":
        messages.append(
            {
                "role": "tool",
                "tool_call_id": str(item.get("call_id") or item.get("id") or ""),
                "content": _tool_output(item.get("output")),
            }
        )
        return
    if role in {"user", "assistant", "system", "developer"} or item_type == "message":
        resolved_role = (
            role if role in {"user", "assistant", "system", "developer"} else "assistant"
        )
        content = _content_text(item.get("content"))
        if content:
            messages.append({"role": resolved_role, "content": content})


def _latest_sdk_items(events: list[dict[str, Any]], user_text: str) -> list[dict[str, Any]]:
    traces = [event["payload"].get("items", []) for event in events if event["kind"] == "sdk_trace"]
    if not traces:
        return []
    items = [item for item in traces[-1] if isinstance(item, dict)]
    start = 0
    for index, item in enumerate(items):
        if item.get("role") == "user" and user_text.strip() in _content_text(item.get("content")):
            start = index
    return items[start:]


def messages_for_run(
    store: Any, run: dict[str, Any], target: str | None = None
) -> list[dict[str, Any]]:
    events = store.events_for_run(run["id"])
    messages: list[dict[str, Any]] = []
    sdk_items = _latest_sdk_items(events, run["user_text"])
    if sdk_items:
        for item in sdk_items:
            _append_item(messages, item)
    else:
        messages.append({"role": "user", "content": run["user_text"]})
        for event in events:
            payload = event["payload"]
            if event["kind"] == "model_output":
                for item in payload.get("output", []):
                    if isinstance(item, dict):
                        _append_item(messages, item)
            elif event["kind"] == "realtime_metadata":
                for item in payload.get("items", []):
                    if isinstance(item, dict):
                        _append_item(messages, item)
            elif event["kind"] == "tool_call" and not any(
                message.get("tool_calls")
                and message["tool_calls"][0]["id"] == payload.get("call_id")
                for message in messages
            ):
                _append_item(
                    messages,
                    {
                        "type": "function_call",
                        "name": payload.get("tool"),
                        "arguments": payload.get("arguments"),
                        "call_id": payload.get("call_id"),
                    },
                )
            elif event["kind"] == "tool_result":
                call_id = payload.get("call_id") or ""
                if not call_id:
                    for message in reversed(messages):
                        if message.get("tool_calls"):
                            call_id = message["tool_calls"][0]["id"]
                            break
                _append_item(
                    messages,
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": payload.get("output"),
                    },
                )

    desired = target if target is not None else (run.get("assistant_text") or "")
    if desired:
        last_text_index = next(
            (
                index
                for index in range(len(messages) - 1, -1, -1)
                if messages[index].get("role") == "assistant" and "content" in messages[index]
            ),
            None,
        )
        if last_text_index is None:
            messages.append({"role": "assistant", "content": desired})
        else:
            messages[last_text_index] = {"role": "assistant", "content": desired}
    if not messages or messages[0].get("role") != "user":
        messages.insert(0, {"role": "user", "content": run["user_text"]})
    return messages


def _observable_spans(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for event in events:
        payload = event["payload"]
        if event["kind"] == "model_output":
            payload = dict(payload)
            output = []
            for item in payload.get("output", []):
                if isinstance(item, dict) and item.get("type") == "reasoning":
                    continue
                if isinstance(item, dict):
                    item = {key: value for key, value in item.items() if key != "encrypted_content"}
                output.append(item)
            payload["output"] = output
        elif event["kind"] == "sdk_trace":
            payload = dict(payload)
            payload["items"] = [
                {key: value for key, value in item.items() if key != "encrypted_content"}
                for item in payload.get("items", [])
                if isinstance(item, dict) and item.get("type") != "reasoning"
            ]
        spans.append(
            {
                "span_id": f"event-{event['id']}",
                "kind": event["kind"],
                "timestamp": event["created_at"],
                "status": "error" if event["kind"] == "error" else "completed",
                "data": payload,
            }
        )
    return spans


def trajectory_v2(store: Any, run: dict[str, Any]) -> dict[str, Any]:
    events = store.events_for_run(run["id"])
    context = store.run_context(run["id"]) or {}
    scores = store.scores_for_run(run["id"])
    target = run.get("correction") or run.get("assistant_text") or ""
    messages = messages_for_run(store, run, target)
    privacy = sensitive_reasons({"messages": messages, "events": events})
    task = store.task_for_run(run["id"])
    return {
        "schema": TRACE_SCHEMA,
        "episode_id": run["id"],
        "group_id": (task or {}).get("id") or f"{run['session_id']}:{run['created_at'][:10]}",
        "session_id": run["session_id"],
        "task_id": (task or {}).get("id"),
        "created_at": run["created_at"],
        "completed_at": run.get("completed_at"),
        "status": run["status"],
        "model": run["model"],
        "interface": context.get("interface")
        or ("realtime_voice" if "realtime" in run["model"] else "agent"),
        "harness": context,
        "messages": messages,
        "spans": _observable_spans(events),
        "outcome": {
            "answer": run.get("assistant_text"),
            "error": run.get("error"),
            "scores": scores,
        },
        "feedback": {
            "rating": run.get("rating"),
            "correction": run.get("correction"),
            "target": target,
        },
        "privacy": {
            "contains_sensitive_patterns": bool(privacy),
            "reasons": privacy,
            "raw_audio_stored": False,
            "hidden_reasoning_included": False,
        },
    }


def agent_sft_row(store: Any, run: dict[str, Any]) -> dict[str, Any]:
    context = store.run_context(run["id"]) or {}
    target = run.get("correction") or run.get("assistant_text") or ""
    return {
        "schema": AGENT_SFT_SCHEMA,
        "messages": messages_for_run(store, run, target),
        "tools": context.get("tools", []),
        "metadata": {
            "run_id": run["id"],
            "model": run["model"],
            "harness_sha256": context.get("harness_sha256"),
            "source": "human_corrected" if run.get("correction") else "human_approved",
        },
    }


def preference_row(store: Any, run: dict[str, Any]) -> dict[str, Any] | None:
    correction = run.get("correction")
    rejected = run.get("assistant_text")
    if not correction or not rejected or correction.strip() == rejected.strip():
        return None
    context = store.run_context(run["id"]) or {}
    prompt_messages = messages_for_run(store, run, target="")
    while (
        prompt_messages
        and prompt_messages[-1].get("role") == "assistant"
        and "content" in prompt_messages[-1]
    ):
        prompt_messages.pop()
    return {
        "schema": PREFERENCE_SCHEMA,
        "prompt": prompt_messages,
        "chosen": correction,
        "rejected": rejected,
        "tools": context.get("tools", []),
        "metadata": {"run_id": run["id"], "harness_sha256": context.get("harness_sha256")},
    }


def split_group(store: Any, run: dict[str, Any]) -> str:
    task = store.task_for_run(run["id"])
    if task:
        return f"task:{task['id']}"
    return f"session-day:{run['session_id']}:{run['created_at'][:10]}"


def utc_now() -> str:
    return datetime.now().astimezone().isoformat()
