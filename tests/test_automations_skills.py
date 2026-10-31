from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jarvis.automations import AutomationStore
from jarvis.skills import SkillStore


def test_once_automation_claims_and_completes(tmp_path: Path) -> None:
    store = AutomationStore(tmp_path / "jarvis.db")
    due = datetime.now(UTC) - timedelta(seconds=1)
    item = store.create_once("session", "Check notes", "Summarize notes", due.isoformat())

    claimed = store.claim_due()
    assert [row["id"] for row in claimed] == [item["id"]]
    store.attach_task(item["id"], "task-1")
    completed = store.complete_for_task("task-1", "All clear")

    assert completed["status"] == "completed"
    assert completed["last_result"] == "All clear"


def test_interval_automation_rearms_after_completion(tmp_path: Path) -> None:
    store = AutomationStore(tmp_path / "jarvis.db")
    item = store.create_interval("session", "Monitor", "Check status", 300)
    store._conn.execute(
        "UPDATE automations SET next_run_at = ? WHERE id = ?",
        ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), item["id"]),
    )
    store._conn.commit()

    store.claim_due()
    store.attach_task(item["id"], "task-2")
    updated = store.complete_for_task("task-2", "No change")

    assert updated["status"] == "active"
    assert datetime.fromisoformat(updated["next_run_at"]) > datetime.now(UTC)


def test_daily_automation_validates_timezone(tmp_path: Path) -> None:
    store = AutomationStore(tmp_path / "jarvis.db")
    item = store.create_daily("session", "Brief", "Prepare brief", "08:30", "America/Los_Angeles")
    assert item["schedule_type"] == "daily"
    with pytest.raises(ValueError):
        store.create_daily("session", "Bad", "No", "08:30", "Mars/Olympus")


def test_skills_are_seeded_and_name_is_confined(tmp_path: Path) -> None:
    skills = SkillStore(tmp_path / "skills")
    names = {item["name"] for item in skills.list()}
    assert {"morning-brief", "file-organizer", "meeting-prep", "research"} <= names
    saved = skills.save("my-workflow", "# My workflow\n\nBe concise.")
    assert saved["name"] == "my-workflow"
    with pytest.raises(ValueError):
        skills.save("../escape", "bad")
