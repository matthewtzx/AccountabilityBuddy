"""SQLite storage for AccountabilityBuddy.

Every write commits straight away, so nothing is lost on a restart (NFR-02).
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Iterable, Optional

from logic import Person, TaskView

DB_PATH = Path(os.getenv("ACCOUNTABILITY_DB_PATH", "accountability.db"))


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def _run(sql: str, params: Iterable = ()) -> None:
    with connect() as connection:
        connection.execute(sql, tuple(params))


def _all(sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
    with connect() as connection:
        return connection.execute(sql, tuple(params)).fetchall()


def _one(sql: str, params: Iterable = ()) -> Optional[sqlite3.Row]:
    with connect() as connection:
        return connection.execute(sql, tuple(params)).fetchone()


# ---------------------------------------------------------------- schema

def initialize_db() -> None:
    with connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                task_number INTEGER,
                name TEXT NOT NULL,
                schedule_type TEXT NOT NULL,
                interval_days INTEGER,
                weekdays TEXT,
                reminder_time TEXT NOT NULL,
                start_date TEXT NOT NULL,
                last_reminded_date TEXT,
                last_completed_date TEXT,
                active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS group_members (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                username TEXT,
                last_seen TEXT NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS task_members (
                task_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                PRIMARY KEY (task_id, user_id),
                FOREIGN KEY (task_id) REFERENCES tasks (id)
            );
            CREATE TABLE IF NOT EXISTS completions (
                task_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                due_date TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                PRIMARY KEY (task_id, user_id, due_date)
            );
            CREATE TABLE IF NOT EXISTS overview_runs (
                chat_id INTEGER NOT NULL,
                run_date TEXT NOT NULL,
                PRIMARY KEY (chat_id, run_date)
            );
            CREATE TABLE IF NOT EXISTS posted_messages (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                task_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                due_date TEXT NOT NULL,
                PRIMARY KEY (chat_id, message_id, task_id)
            );
            CREATE INDEX IF NOT EXISTS posted_messages_by_task
                ON posted_messages (task_id, due_date);
            CREATE TABLE IF NOT EXISTS due_log (
                task_id INTEGER NOT NULL,
                due_date TEXT NOT NULL,
                PRIMARY KEY (task_id, due_date)
            );
            """
        )
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(tasks)")}
        if "task_number" not in columns:
            connection.execute("ALTER TABLE tasks ADD COLUMN task_number INTEGER")
        if "created_date" not in columns:
            connection.execute("ALTER TABLE tasks ADD COLUMN created_date TEXT")
        connection.execute("UPDATE tasks SET created_date = start_date WHERE created_date IS NULL")
        # NFR-07: only user ID, display name and username are kept.
        connection.execute("UPDATE group_members SET last_seen = '' WHERE last_seen != ''")
        # Older tasks had no member list; assign them to their creator.
        connection.execute(
            "INSERT OR IGNORE INTO task_members (task_id, user_id) SELECT id, user_id FROM tasks"
        )
        for chat in connection.execute("SELECT DISTINCT chat_id FROM tasks WHERE active = 1").fetchall():
            _renumber(connection, chat["chat_id"])


def _renumber(connection: sqlite3.Connection, chat_id: int) -> None:
    ids = connection.execute(
        "SELECT id FROM tasks WHERE chat_id = ? AND active = 1 ORDER BY task_number, id", (chat_id,)
    ).fetchall()
    connection.executemany(
        "UPDATE tasks SET task_number = ? WHERE id = ?",
        [(number, row["id"]) for number, row in enumerate(ids, start=1)],
    )


# ---------------------------------------------------------------- members

def join_member(chat_id: int, user_id: int, display_name: str, username: Optional[str]) -> None:
    _run(
        """
        INSERT INTO group_members (chat_id, user_id, display_name, username, last_seen)
        VALUES (?, ?, ?, ?, '')
        ON CONFLICT(chat_id, user_id) DO UPDATE SET
            display_name = excluded.display_name, username = excluded.username
        """,
        (chat_id, user_id, display_name, username),
    )


def refresh_member(chat_id: int, user_id: int, display_name: str, username: Optional[str]) -> None:
    """Keep a joined member's name current; never registers anyone new."""
    _run(
        """
        UPDATE group_members SET display_name = ?, username = ?
        WHERE chat_id = ? AND user_id = ? AND (display_name != ? OR username IS NOT ?)
        """,
        (display_name, username, chat_id, user_id, display_name, username),
    )


def list_members(chat_id: int) -> list[sqlite3.Row]:
    return _all(
        "SELECT user_id, display_name FROM group_members WHERE chat_id = ? "
        "ORDER BY display_name COLLATE NOCASE",
        (chat_id,),
    )


# ---------------------------------------------------------------- tasks

def create_task(chat_id: int, creator_id: int, name: str, schedule_type: str,
                interval_days: Optional[int], weekdays: Optional[str], reminder_time: str,
                today: str, assignees: Iterable[int], skip_today_reminder: bool) -> sqlite3.Row:
    with connect() as connection:
        number = connection.execute(
            "SELECT COALESCE(MAX(task_number), 0) + 1 FROM tasks WHERE chat_id = ? AND active = 1",
            (chat_id,),
        ).fetchone()[0]
        cursor = connection.execute(
            """
            INSERT INTO tasks (chat_id, user_id, task_number, name, schedule_type, interval_days,
                               weekdays, reminder_time, start_date, created_date, last_reminded_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (chat_id, creator_id, number, name, schedule_type, interval_days, weekdays,
             reminder_time, today, today, today if skip_today_reminder else None),
        )
        connection.executemany(
            "INSERT INTO task_members (task_id, user_id) VALUES (?, ?)",
            [(cursor.lastrowid, user_id) for user_id in assignees],
        )
        return connection.execute("SELECT * FROM tasks WHERE id = ?", (cursor.lastrowid,)).fetchone()


def get_task(task_id: int) -> Optional[sqlite3.Row]:
    return _one("SELECT * FROM tasks WHERE id = ?", (task_id,))


def get_task_by_number(chat_id: int, number: int) -> Optional[sqlite3.Row]:
    return _one(
        "SELECT * FROM tasks WHERE chat_id = ? AND task_number = ? AND active = 1", (chat_id, number)
    )


def active_tasks(chat_id: Optional[int] = None) -> list[sqlite3.Row]:
    if chat_id is None:
        return _all("SELECT * FROM tasks WHERE active = 1 ORDER BY chat_id, task_number")
    return _all("SELECT * FROM tasks WHERE active = 1 AND chat_id = ? ORDER BY task_number", (chat_id,))


def task_member_ids(task_id: int) -> set[int]:
    return {row["user_id"] for row in _all("SELECT user_id FROM task_members WHERE task_id = ?", (task_id,))}


def task_view(task: sqlite3.Row, due_date: str) -> TaskView:
    rows = _all(
        """
        SELECT tm.user_id, COALESCE(gm.display_name, 'User ' || tm.user_id) AS display_name,
               c.completed_at IS NOT NULL AS done
        FROM task_members tm
        LEFT JOIN group_members gm ON gm.chat_id = ? AND gm.user_id = tm.user_id
        LEFT JOIN completions c
            ON c.task_id = tm.task_id AND c.user_id = tm.user_id AND c.due_date = ?
        WHERE tm.task_id = ?
        ORDER BY display_name COLLATE NOCASE
        """,
        (task["chat_id"], due_date, task["id"]),
    )
    people = [Person(row["user_id"], row["display_name"], bool(row["done"])) for row in rows]
    return TaskView(task["id"], task["name"], task["reminder_time"], people)


def update_task(task_id: int, fields: dict, assignees: Optional[Iterable[int]] = None) -> None:
    with connect() as connection:
        if fields:
            assignments = ", ".join(f"{column} = ?" for column in fields)
            connection.execute(
                f"UPDATE tasks SET {assignments} WHERE id = ?", (*fields.values(), task_id)
            )
        if assignees is not None:
            connection.execute("DELETE FROM task_members WHERE task_id = ?", (task_id,))
            connection.executemany(
                "INSERT INTO task_members (task_id, user_id) VALUES (?, ?)",
                [(task_id, user_id) for user_id in assignees],
            )


def delete_task(task_id: int) -> None:
    """Soft-delete so past completions stay readable, then close the numbering gap."""
    with connect() as connection:
        row = connection.execute("SELECT chat_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
        connection.execute("UPDATE tasks SET active = 0, task_number = NULL WHERE id = ?", (task_id,))
        if row is not None:
            _renumber(connection, row["chat_id"])


def mark_reminded(task_id: int, day: str) -> None:
    _run("UPDATE tasks SET last_reminded_date = ? WHERE id = ?", (day, task_id))


# ---------------------------------------------------------------- completions

def toggle_completion(task_id: int, user_id: int, due_date: str, completed_at: str) -> bool:
    """Tick if not ticked, untick if ticked. Returns True when now ticked."""
    with connect() as connection:
        deleted = connection.execute(
            "DELETE FROM completions WHERE task_id = ? AND user_id = ? AND due_date = ?",
            (task_id, user_id, due_date),
        ).rowcount
        if deleted:
            return False
        connection.execute(
            "INSERT INTO completions (task_id, user_id, due_date, completed_at) VALUES (?, ?, ?, ?)",
            (task_id, user_id, due_date, completed_at),
        )
        return True


def tick(task_id: int, user_id: int, due_date: str, completed_at: str) -> bool:
    """Tick only. Returns False when it was already ticked."""
    with connect() as connection:
        return connection.execute(
            "INSERT OR IGNORE INTO completions (task_id, user_id, due_date, completed_at) "
            "VALUES (?, ?, ?, ?)",
            (task_id, user_id, due_date, completed_at),
        ).rowcount == 1


# ---------------------------------------------------------------- scheduling records

def overview_done(chat_id: int, day: str) -> bool:
    return _one("SELECT 1 FROM overview_runs WHERE chat_id = ? AND run_date = ?", (chat_id, day)) is not None


def mark_overview_done(chat_id: int, day: str) -> None:
    _run("INSERT OR IGNORE INTO overview_runs (chat_id, run_date) VALUES (?, ?)", (chat_id, day))


def log_due(task_id: int, day: str) -> None:
    _run("INSERT OR IGNORE INTO due_log (task_id, due_date) VALUES (?, ?)", (task_id, day))


def record_message(chat_id: int, message_id: int, task_ids: Iterable[int], kind: str, due_date: str) -> None:
    with connect() as connection:
        connection.executemany(
            "INSERT OR IGNORE INTO posted_messages (chat_id, message_id, task_id, kind, due_date) "
            "VALUES (?, ?, ?, ?, ?)",
            [(chat_id, message_id, task_id, kind, due_date) for task_id in task_ids],
        )


def messages_for(task_id: int, due_date: str) -> list[sqlite3.Row]:
    return _all(
        "SELECT chat_id, message_id, kind FROM posted_messages WHERE task_id = ? AND due_date = ?",
        (task_id, due_date),
    )


def tasks_in_message(chat_id: int, message_id: int) -> list[sqlite3.Row]:
    """Tasks shown in a posted message, in the order they were posted."""
    return _all(
        """
        SELECT t.* FROM posted_messages pm JOIN tasks t ON t.id = pm.task_id
        WHERE pm.chat_id = ? AND pm.message_id = ? ORDER BY pm.rowid
        """,
        (chat_id, message_id),
    )


def migrate_chat(old_chat_id: int, new_chat_id: int) -> None:
    """A group upgraded to a supergroup gets a new chat ID; move its data across."""
    with connect() as connection:
        connection.execute("UPDATE tasks SET chat_id = ? WHERE chat_id = ?", (new_chat_id, old_chat_id))
        connection.execute(
            "UPDATE OR IGNORE group_members SET chat_id = ? WHERE chat_id = ?", (new_chat_id, old_chat_id)
        )
        connection.execute("DELETE FROM group_members WHERE chat_id = ?", (old_chat_id,))
        connection.execute(
            "UPDATE OR IGNORE overview_runs SET chat_id = ? WHERE chat_id = ?", (new_chat_id, old_chat_id)
        )
        connection.execute("DELETE FROM overview_runs WHERE chat_id = ?", (old_chat_id,))
        # Old messages cannot be edited in the new chat.
        connection.execute("DELETE FROM posted_messages WHERE chat_id = ?", (old_chat_id,))
