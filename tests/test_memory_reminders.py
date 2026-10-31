from datetime import UTC, datetime, timedelta
from pathlib import Path

from jarvis.memory import MemoryStore
from jarvis.reminders import ReminderStore


def test_memory_is_explicit_recallable_and_soft_deleted(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "jarvis.db")
    saved = store.remember("I prefer concise answers", kind="preference")

    recalled = store.recall("concise answers")
    assert [item["id"] for item in recalled] == [saved["id"]]
    assert store.forget(saved["id"])
    assert store.recall("concise answers") == []
    assert store.get(saved["id"])["active"] is False


def test_one_shot_reminder_fires_once(tmp_path: Path) -> None:
    store = ReminderStore(tmp_path / "jarvis.db")
    due = datetime.now(UTC) - timedelta(seconds=1)
    reminder = store.create("Stand up", due.isoformat())

    claimed = store.claim_due()
    assert [item["id"] for item in claimed] == [reminder["id"]]
    store.mark_fired(reminder["id"])

    assert store.get(reminder["id"])["status"] == "completed"
    assert store.claim_due() == []


def test_repeating_reminder_advances_to_future(tmp_path: Path) -> None:
    store = ReminderStore(tmp_path / "jarvis.db")
    due = datetime.now(UTC) - timedelta(minutes=5)
    reminder = store.create("Hydrate", due.isoformat(), repeat_seconds=60)

    store.claim_due()
    updated = store.mark_fired(reminder["id"])

    assert updated["status"] == "scheduled"
    assert datetime.fromisoformat(updated["due_at"]) > datetime.now(UTC)


def test_reminder_requires_timezone(tmp_path: Path) -> None:
    store = ReminderStore(tmp_path / "jarvis.db")
    try:
        store.create("Ambiguous", "2026-10-29T10:00:00")
    except ValueError as exc:
        assert "timezone" in str(exc)
    else:
        raise AssertionError("naive reminder time was accepted")
