import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jarvis.config import Settings
from jarvis.core import JarvisCore
from jarvis.sdk_agent import SDKTurn
from jarvis.store import TraceStore


class FakeSDKAgent:
    def __init__(self, approval: bool = False) -> None:
        self.approval = approval

    async def chat(self, session_id: str, text: str) -> SDKTurn:
        del session_id, text
        if self.approval:
            return SDKTurn(
                status="approval_required",
                checkpoint={"saved": True},
                tool_name="write_file",
                arguments={"path": "/tmp/result.txt", "content": "done"},
                call_id="call-1",
            )
        return SDKTurn(status="completed", message="finished")

    async def resume(self, session_id: str, checkpoint: dict, approved: bool) -> SDKTurn:
        del session_id
        assert checkpoint == {"saved": True}
        return SDKTurn(status="completed", message="approved" if approved else "denied")


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        openai_api_key="test",
        jarvis_allowed_roots=[tmp_path],
        jarvis_db_path=tmp_path / "jarvis.db",
        jarvis_skills_path=tmp_path / "skills",
        jarvis_enable_web_search=False,
    )


@pytest.mark.asyncio
async def test_durable_task_completes(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    store = TraceStore(settings.jarvis_db_path)
    session = store.create_session()
    core = JarvisCore(settings, store=store, agent=FakeSDKAgent())  # type: ignore[arg-type]

    result = await core.submit(session, "do the work")

    assert result["status"] == "completed"
    task = store.get_task(result["task_id"])
    assert task is not None
    assert task["status"] == "completed"
    assert task["result"]["message"] == "finished"


@pytest.mark.asyncio
async def test_task_approval_is_persisted_and_resumed(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    store = TraceStore(settings.jarvis_db_path)
    session = store.create_session()
    core = JarvisCore(settings, store=store, agent=FakeSDKAgent(approval=True))  # type: ignore[arg-type]

    pending = await core.submit(session, "write the result")
    assert pending["status"] == "approval_required"
    approval_id = pending["approval"]["id"]
    assert store.get_task_approval(approval_id)["checkpoint"] == {"saved": True}

    completed = await core.resolve_approval(approval_id, True)
    assert completed["status"] == "completed"
    assert completed["message"] == "approved"
    assert store.get_task(completed["task_id"])["status"] == "completed"


@pytest.mark.asyncio
async def test_confined_restart_reemits_pending_request_for_supervisor_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    store = TraceStore(settings.jarvis_db_path)
    session = store.create_session()
    original = JarvisCore(settings, store=store, agent=FakeSDKAgent(approval=True))
    pending = await original.submit(session, "write the result")
    events = []

    async def capture(event):
        events.append(event)

    monkeypatch.setenv("JARVIS_CONFINED_AGENT", "1")
    restarted = JarvisCore(settings, store=store, agent=FakeSDKAgent(), event_sink=capture)
    await restarted.start()
    try:
        recovered = next(
            event for event in events if event.get("approval_id") == pending["approval"]["id"]
        )
        assert recovered["tool_name"] == "write_file"
        assert recovered["arguments"]["content"] == "done"
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_confined_restart_does_not_replay_lost_patch_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class PatchAgent(FakeSDKAgent):
        async def chat(self, session_id: str, text: str) -> SDKTurn:
            return SDKTurn(
                status="approval_required",
                checkpoint={"saved": True},
                tool_name="apply_reviewed_patch",
                arguments={"patch_id": "lost", "sha256": "0" * 64},
                call_id="call-1",
            )

    settings = make_settings(tmp_path)
    store = TraceStore(settings.jarvis_db_path)
    session = store.create_session()
    original = JarvisCore(settings, store=store, agent=PatchAgent())
    pending = await original.submit(session, "apply the patch")
    monkeypatch.setenv("JARVIS_CONFINED_AGENT", "1")
    restarted = JarvisCore(settings, store=store, agent=FakeSDKAgent())
    await restarted.start()
    try:
        assert store.get_task(pending["task_id"])["status"] == "failed"
        assert store.get_task_approval(pending["approval"]["id"])["status"] == "denied"
    finally:
        await restarted.close()


def test_restart_recovery_only_requeues_safe_tasks(tmp_path: Path) -> None:
    store = TraceStore(tmp_path / "jarvis.db")
    session = store.create_session()
    safe = store.create_task(session, "read files")
    safe_run = store.create_run(session, "read files", "test")
    store.start_task(safe, safe_run, auto_retry=True)
    unsafe = store.create_task(session, "run command")
    unsafe_run = store.create_run(session, "run command", "test")
    store.start_task(unsafe, unsafe_run, auto_retry=False)

    recovered = store.prepare_task_recovery()

    assert [task["id"] for task in recovered] == [safe]
    assert store.get_task(safe)["status"] == "queued"
    assert store.get_task(unsafe)["status"] == "interrupted"


@pytest.mark.asyncio
async def test_scheduled_automation_runs_through_durable_core(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    store = TraceStore(settings.jarvis_db_path)
    session = store.create_session()
    events: list[dict] = []

    async def sink(event: dict) -> None:
        events.append(event)

    core = JarvisCore(
        settings,
        store=store,
        agent=FakeSDKAgent(),  # type: ignore[arg-type]
        event_sink=sink,
    )
    core.automations.create_once(
        session,
        "Test automation",
        "do the work",
        (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
    )
    await core.start()
    await asyncio.sleep(2.2)
    await core.close()

    assert any(event["status"] == "automation_completed" for event in events)
    assert core.automations.list(include_finished=True)[0]["status"] == "completed"
