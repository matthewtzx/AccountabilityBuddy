"""Pure logic for AccountabilityBuddy: parsing, schedules and message rendering.

Nothing here touches the database or the network, so it can be unit tested.
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Mapping, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
DAY_ALIASES = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2, "thu": 3, "thur": 3, "thurs": 3,
    "thursday": 3, "fri": 4, "friday": 4, "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}

MAX_TASK_NAME = 100
MAX_MESSAGE_CHARS = 4000  # Telegram allows 4,096; keep a margin.
MAX_BUTTONS = 90          # Telegram allows about 100 inline buttons per message.


# ---------------------------------------------------------------- parsing

def parse_name(text: str) -> Optional[str]:
    name = " ".join(text.split())
    if not name or len(name) > MAX_TASK_NAME:
        return None
    return name


def parse_interval(text: str) -> Optional[int]:
    value = text.strip()
    if not value.isdigit():
        return None
    number = int(value)
    return number if number >= 1 else None


def parse_weekdays(text: str) -> Optional[str]:
    """Return weekdays as a sorted comma string such as "0,2,4", or None."""
    pieces = [piece.strip().lower() for piece in text.replace(";", ",").split(",")]
    pieces = [piece for piece in pieces if piece]
    if not pieces or any(piece not in DAY_ALIASES for piece in pieces):
        return None
    return ",".join(str(day) for day in sorted({DAY_ALIASES[piece] for piece in pieces}))


def parse_time(text: str) -> Optional[str]:
    try:
        return datetime.strptime(text.strip(), "%H:%M").strftime("%H:%M")
    except ValueError:
        return None


# ---------------------------------------------------------------- schedules

def task_is_due(task: Mapping, day: date) -> bool:
    if task["schedule_type"] == "interval":
        start = date.fromisoformat(task["start_date"])
        return day >= start and (day - start).days % task["interval_days"] == 0
    weekdays = {int(value) for value in task["weekdays"].split(",")}
    return day.weekday() in weekdays


def schedule_description(task: Mapping) -> str:
    if task["schedule_type"] == "interval":
        days = task["interval_days"]
        return "every day" if days == 1 else f"every {days} days"
    return "on " + ", ".join(DAY_NAMES[int(value)] for value in task["weekdays"].split(","))


def reminder_passed(now: datetime, reminder_time: str) -> bool:
    return now.strftime("%H:%M") >= reminder_time


def day_code(day: str) -> str:
    """ISO date to the compact form used in button data."""
    return day.replace("-", "")


def day_from_code(code: str) -> str:
    return f"{code[0:4]}-{code[4:6]}-{code[6:8]}"


# ---------------------------------------------------------------- rendering

@dataclass
class Person:
    user_id: int
    name: str
    done: bool = False


@dataclass
class TaskView:
    task_id: int
    name: str
    reminder_time: str
    people: list[Person] = field(default_factory=list)

    @property
    def everyone_done(self) -> bool:
        return bool(self.people) and all(person.done for person in self.people)


def tick_button(task: TaskView, person: Person, day: str, with_task_name: bool) -> InlineKeyboardButton:
    mark = "✅" if person.done else "☐"
    label = f"{mark} {person.name}"
    if with_task_name:
        label += f" · {task.name}"
    return InlineKeyboardButton(
        label[:64], callback_data=f"t:{task.task_id}:{day_code(day)}:{person.user_id}"
    )


def render_reminder(task: TaskView, day: str) -> tuple[str, InlineKeyboardMarkup]:
    lines = [f"⏰ time for <b>{html.escape(task.name)}</b> ({task.reminder_time})", ""]
    lines += [f"{'✅' if p.done else '⏳'} {html.escape(p.name)}" for p in task.people]
    lines += ["", "tap your own name when you're done."]
    keyboard = [[tick_button(task, person, day, False)] for person in task.people]
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


def _overview_block(task: TaskView) -> str:
    people = ", ".join(html.escape(p.name) for p in task.people) or "nobody"
    return f"<b>• {html.escape(task.name)}</b>\n⏰ {task.reminder_time} · 👥 {people}"


def render_overview(tasks: list[TaskView], day: str, timezone_name: str,
                    part: int = 1, parts: int = 1) -> tuple[str, InlineKeyboardMarkup]:
    heading_day = date.fromisoformat(day).strftime("%A %d %b")
    heading = f"🌅 <b>today's tasks</b> · {heading_day}"
    if parts > 1:
        heading += f" ({part}/{parts})"
    lines = [heading, ""]
    for task in tasks:
        lines += [_overview_block(task), ""]
    lines.append(f"times are {timezone_name}. tap your own name when you're done.")
    with_name = len(tasks) > 1
    keyboard = [
        [tick_button(task, person, day, with_name)]
        for task in tasks for person in task.people
    ]
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


def chunk_overview(tasks: list[TaskView]) -> list[list[TaskView]]:
    """Group tasks so each overview message stays under Telegram's limits."""
    chunks: list[list[TaskView]] = []
    current: list[TaskView] = []
    chars = buttons = 0
    for task in tasks:
        task_chars = len(_overview_block(task)) + 2
        task_buttons = len(task.people)
        if current and (chars + task_chars > MAX_MESSAGE_CHARS - 300
                        or buttons + task_buttons > MAX_BUTTONS):
            chunks.append(current)
            current, chars, buttons = [], 0, 0
        current.append(task)
        chars += task_chars
        buttons += task_buttons
    if current:
        chunks.append(current)
    return chunks


def split_text(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Split plain text on line breaks into pieces no longer than limit."""
    pieces: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            pieces.append(current)
            current = line
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces
