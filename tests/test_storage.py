import shutil
import sqlite3
from pathlib import Path

import pytest

import storage
from conftest import CHAT, A, B, make_task


def numbers():
    return [(t["task_number"], t["name"]) for t in storage.active_tasks(CHAT)]


def test_join_twice_does_not_duplicate_tc03():
    storage.join_member(CHAT, A, "Alice", None)
    assert [m["user_id"] for m in storage.list_members(CHAT)].count(A) == 1


def test_refresh_never_registers_new_people_tc10():
    storage.refresh_member(CHAT, 99, "Stranger", None)
    assert 99 not in {m["user_id"] for m in storage.list_members(CHAT)}
    storage.refresh_member(CHAT, A, "Alice Tan", "alice")
    assert "Alice Tan" in {m["display_name"] for m in storage.list_members(CHAT)}


def test_numbers_close_gap_after_delete_tc13_tc37():
    first = make_task("one")
    make_task("two")
    make_task("three")
    assert numbers() == [(1, "one"), (2, "two"), (3, "three")]
    storage.delete_task(first["id"])
    assert numbers() == [(1, "two"), (2, "three")]
    assert make_task("four")["task_number"] == 3


def test_tasks_are_per_chat_tc40():
    make_task("group 1 task")
    other = make_task("group 2 task", chat=-200)
    assert other["task_number"] == 1
    assert [t["name"] for t in storage.active_tasks(CHAT)] == ["group 1 task"]


def test_toggle_and_view_tc25_tc28():
    task = make_task()
    assert storage.toggle_completion(task["id"], A, "2026-11-17", "t") is True
    people = {p.name: p.done for p in storage.task_view(task, "2026-11-17").people}
    assert people == {"Alice": True, "Bob": False}
    assert storage.toggle_completion(task["id"], A, "2026-11-17", "t") is False
    assert not any(p.done for p in storage.task_view(task, "2026-11-17").people)


def test_edit_keeps_past_completions_tc36():
    task = make_task()
    storage.toggle_completion(task["id"], A, "2026-11-17", "t")
    storage.update_task(task["id"], {"name": "Evening devo", "reminder_time": "22:00"})
    view = storage.task_view(storage.get_task(task["id"]), "2026-11-17")
    assert view.name == "Evening devo" and view.people[0].done


def test_edit_people_tc35():
    task = make_task()
    storage.update_task(task["id"], {}, {A, 3})
    assert storage.task_member_ids(task["id"]) == {A, 3}


def test_messages_are_tracked_per_task_and_day():
    one, two = make_task("one"), make_task("two")
    storage.record_message(CHAT, 50, [one["id"], two["id"]], "overview 1/1", "2026-11-17")
    storage.record_message(CHAT, 51, [one["id"]], "reminder", "2026-11-17")
    assert {r["message_id"] for r in storage.messages_for(one["id"], "2026-11-17")} == {50, 51}
    assert [t["name"] for t in storage.tasks_in_message(CHAT, 50)] == ["one", "two"]


def test_supergroup_migration_moves_everything():
    task = make_task()
    storage.mark_overview_done(CHAT, "2026-11-17")
    storage.migrate_chat(CHAT, -999)
    assert storage.get_task(task["id"])["chat_id"] == -999
    assert len(storage.list_members(-999)) == 3 and storage.list_members(CHAT) == []
    assert storage.overview_done(-999, "2026-11-17")


def test_existing_database_upgrades(tmp_path, monkeypatch):
    """The DB from the deployed skeleton opens, keeps its rows, and gains the new tables."""
    source = Path(__file__).resolve().parent.parent / "accountability.db"
    if not source.exists():
        pytest.skip("no local accountability.db to test against")
    copy = tmp_path / "old.db"
    shutil.copy(source, copy)
    before = sqlite3.connect(copy).execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    monkeypatch.setattr(storage, "DB_PATH", copy)
    storage.initialize_db()
    storage.initialize_db()  # running twice is harmless
    connection = sqlite3.connect(copy)
    assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before
    assert connection.execute("SELECT COUNT(*) FROM tasks WHERE created_date IS NULL").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM group_members WHERE last_seen != ''").fetchone()[0] == 0
    for table in ("overview_runs", "posted_messages", "due_log"):
        connection.execute(f"SELECT * FROM {table}")
