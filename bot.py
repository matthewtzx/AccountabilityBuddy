import html
import logging
import os
import sqlite3
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
LOGGER = logging.getLogger(__name__)

NAME, SCHEDULE_TYPE, SCHEDULE_VALUE, REMINDER_TIME, ASSIGNEES = range(5)
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
DAY_ALIASES = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2, "thu": 3, "thur": 3, "thurs": 3,
    "thursday": 3, "fri": 4, "friday": 4, "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}

DB_PATH = Path(os.getenv("ACCOUNTABILITY_DB_PATH", "accountability.db"))
TIMEZONE_NAME = os.getenv("BOT_TIMEZONE", "Asia/Singapore")
try:
    BOT_TIMEZONE = ZoneInfo(TIMEZONE_NAME)
except ZoneInfoNotFoundError as exc:
    raise RuntimeError(f"Unknown BOT_TIMEZONE: {TIMEZONE_NAME}") from exc


def connect_db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_db() -> None:
    with connect_db() as connection:
        connection.execute(
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
            )
            """
        )
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(tasks)")}
        if "task_number" not in columns:
            connection.execute("ALTER TABLE tasks ADD COLUMN task_number INTEGER")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS group_members (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                username TEXT,
                last_seen TEXT NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS task_members (
                task_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                PRIMARY KEY (task_id, user_id),
                FOREIGN KEY (task_id) REFERENCES tasks (id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS completions (
                task_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                due_date TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                PRIMARY KEY (task_id, user_id, due_date)
            )
            """
        )
        # Preserve old tasks by assigning them to their original creator.
        connection.execute(
            """
            INSERT OR IGNORE INTO task_members (task_id, user_id)
            SELECT id, user_id FROM tasks
            """
        )
        chats = connection.execute(
            "SELECT DISTINCT chat_id FROM tasks WHERE active = 1"
        ).fetchall()
        for chat in chats:
            task_ids = connection.execute(
                "SELECT id FROM tasks WHERE chat_id = ? AND active = 1 ORDER BY id",
                (chat["chat_id"],),
            ).fetchall()
            connection.executemany(
                "UPDATE tasks SET task_number = ? WHERE id = ?",
                [(number, task["id"]) for number, task in enumerate(task_ids, start=1)],
            )


def remember_user(update: Update) -> None:
    if update.effective_chat is None or update.effective_user is None:
        return
    user = update.effective_user
    with connect_db() as connection:
        connection.execute(
            """
            INSERT INTO group_members (chat_id, user_id, display_name, username, last_seen)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                display_name = excluded.display_name,
                username = excluded.username,
                last_seen = excluded.last_seen
            """,
            (
                update.effective_chat.id, user.id, user.full_name,
                user.username, datetime.now(BOT_TIMEZONE).isoformat(),
            ),
        )


async def register_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    remember_user(update)


async def register_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    remember_user(update)


def assignee_keyboard(chat_id: int, selected: set[int]) -> InlineKeyboardMarkup:
    with connect_db() as connection:
        members = connection.execute(
            "SELECT * FROM group_members WHERE chat_id = ? ORDER BY display_name COLLATE NOCASE",
            (chat_id,),
        ).fetchall()
    rows = []
    for member in members:
        marker = "✅" if member["user_id"] in selected else "☐"
        label = f"{marker} {member['display_name']}"
        rows.append([InlineKeyboardButton(label[:60], callback_data=f"assign:{member['user_id']}")])
    rows.append([InlineKeyboardButton("Done", callback_data="assign:done")])
    return InlineKeyboardMarkup(rows)


def task_is_due(task: sqlite3.Row, day: date) -> bool:
    if task["schedule_type"] == "interval":
        start = date.fromisoformat(task["start_date"])
        return day >= start and (day - start).days % task["interval_days"] == 0
    weekdays = {int(value) for value in task["weekdays"].split(",")}
    return day.weekday() in weekdays


def schedule_description(task: sqlite3.Row) -> str:
    if task["schedule_type"] == "interval":
        days = task["interval_days"]
        return "every day" if days == 1 else f"every {days} days"
    weekdays = [DAY_NAMES[int(value)] for value in task["weekdays"].split(",")]
    return "on " + ", ".join(weekdays)


def completion_status(task: sqlite3.Row, due_date: str) -> tuple[str, bool]:
    with connect_db() as connection:
        members = connection.execute(
            """
            SELECT tm.user_id, COALESCE(gm.display_name, 'User ' || tm.user_id) AS display_name,
                   c.completed_at IS NOT NULL AS completed
            FROM task_members tm
            LEFT JOIN group_members gm
                ON gm.chat_id = ? AND gm.user_id = tm.user_id
            LEFT JOIN completions c
                ON c.task_id = tm.task_id AND c.user_id = tm.user_id AND c.due_date = ?
            WHERE tm.task_id = ?
            ORDER BY display_name COLLATE NOCASE
            """,
            (task["chat_id"], due_date, task["id"]),
        ).fetchall()
    lines = [f"{'✅' if member['completed'] else '⏳'} {html.escape(member['display_name'])}" for member in members]
    return "\n".join(lines), bool(members) and all(member["completed"] for member in members)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "hi! i am AccountabilityBuddy, a bot to help you stay accountable with others!\n\n"
        "each person should send /join once so task creators can select them.\n"
        "use /addtask to create a task or /tasks to see your tasks."
    )


async def join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    remember_user(update)
    await update.message.reply_text(
        f"✅ {update.effective_user.full_name} can now be assigned to tasks in this chat."
    )


async def add_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("new_task", None)
    await update.message.reply_text("what is the task name? send /cancel to stop.")
    return NAME


async def receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("please enter a non-empty task name")
        return NAME
    context.user_data["new_task"] = {"name": name}
    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("every N days", callback_data="schedule:interval"),
            InlineKeyboardButton("specific days of the week", callback_data="schedule:weekly"),
        ]]
    )
    await update.message.reply_text("how often should it be done?", reply_markup=keyboard)
    return SCHEDULE_TYPE


async def choose_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    schedule_type = query.data.split(":", 1)[1]
    context.user_data["new_task"]["schedule_type"] = schedule_type
    prompt = (
        "how many days between each occurrence? send a whole number, such as 2."
        if schedule_type == "interval"
        else "which days? e.g: monday, wednesday, friday"
    )
    await query.edit_message_text(prompt)
    return SCHEDULE_VALUE


async def receive_schedule_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    task = context.user_data["new_task"]
    value = update.message.text.strip()
    if task["schedule_type"] == "interval":
        try:
            interval_days = int(value)
            if interval_days < 1:
                raise ValueError
        except ValueError:
            await update.message.reply_text("enter a whole number of at least 1.")
            return SCHEDULE_VALUE
        task["interval_days"] = interval_days
        task["weekdays"] = None
    else:
        pieces = [piece.strip().lower() for piece in value.replace(";", ",").split(",")]
        try:
            weekdays = sorted({DAY_ALIASES[piece] for piece in pieces if piece})
        except KeyError:
            weekdays = []
        if not weekdays or len(weekdays) != len([piece for piece in pieces if piece]):
            await update.message.reply_text(
                "sorry, i couldn't understand those days :( try separating full or short names with commas, "
                "for example: Mon, Wed, Fri"
            )
            return SCHEDULE_VALUE
        task["interval_days"] = None
        task["weekdays"] = ",".join(str(day) for day in weekdays)

    await update.message.reply_text(
        f"what time should i remind you? use 24-hour HH:MM format in {TIMEZONE_NAME}, e.g: 19:30."
    )
    return REMINDER_TIME


async def receive_reminder_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = update.message.text.strip()
    try:
        parsed_time = datetime.strptime(value, "%H:%M").time()
    except ValueError:
        await update.message.reply_text("use 24-hour HH:MM format, e.g: 07:30 or 19:30.")
        return REMINDER_TIME

    task = context.user_data["new_task"]
    task["reminder_time"] = parsed_time.strftime("%H:%M")
    task["assignees"] = {update.effective_user.id}
    await update.message.reply_text(
        "who should be accountable to this task? tap names to select or deselect them, then tap Done.\n\n"
        "if someone is missing, cancel this setup and ask them to send /join first.",
        reply_markup=assignee_keyboard(update.effective_chat.id, task["assignees"]),
    )
    return ASSIGNEES


async def choose_assignees(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    task = context.user_data["new_task"]
    selection = query.data.split(":", 1)[1]
    if selection != "done":
        user_id = int(selection)
        if user_id in task["assignees"]:
            task["assignees"].remove(user_id)
        else:
            task["assignees"].add(user_id)
        await query.answer()
        await query.edit_message_reply_markup(
            reply_markup=assignee_keyboard(update.effective_chat.id, task["assignees"])
        )
        return ASSIGNEES

    if not task["assignees"]:
        await query.answer("select at least one person.", show_alert=True)
        return ASSIGNEES

    with connect_db() as connection:
        task_number = connection.execute(
            "SELECT COALESCE(MAX(task_number), 0) + 1 FROM tasks WHERE chat_id = ? AND active = 1",
            (update.effective_chat.id,),
        ).fetchone()[0]
        cursor = connection.execute(
            """
            INSERT INTO tasks (
                chat_id, user_id, task_number, name, schedule_type, interval_days,
                weekdays, reminder_time, start_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                update.effective_chat.id, update.effective_user.id, task_number, task["name"],
                task["schedule_type"], task["interval_days"], task["weekdays"],
                task["reminder_time"], datetime.now(BOT_TIMEZONE).date().isoformat(),
            ),
        )
        task_id = cursor.lastrowid
        connection.executemany(
            "INSERT INTO task_members (task_id, user_id) VALUES (?, ?)",
            [(task_id, user_id) for user_id in task["assignees"]],
        )
        saved_task = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        names = connection.execute(
            """
            SELECT display_name FROM group_members
            WHERE chat_id = ? AND user_id IN ({}) ORDER BY display_name COLLATE NOCASE
            """.format(",".join("?" for _ in task["assignees"])),
            (update.effective_chat.id, *task["assignees"]),
        ).fetchall()
    await query.answer()
    context.user_data.pop("new_task", None)
    await query.edit_message_text(
        f"✅ task #{saved_task['task_number']} created!: {saved_task['name']}\n"
        f"schedule: {schedule_description(saved_task)} at {saved_task['reminder_time']} ({TIMEZONE_NAME})\n"
        f"assigned to: {', '.join(row['display_name'] for row in names)}"
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("new_task", None)
    await update.message.reply_text("task creation cancelled :(")
    return ConversationHandler.END


async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with connect_db() as connection:
        tasks = connection.execute(
            "SELECT * FROM tasks WHERE chat_id = ? AND active = 1 ORDER BY id",
            (update.effective_chat.id,),
        ).fetchall()
    if not tasks:
        await update.message.reply_text("you have no active tasks. use /addtask to create one.")
        return
    lines = ["your active tasks:"]
    today = datetime.now(BOT_TIMEZONE).date()
    for task in tasks:
        status, _ = completion_status(task, today.isoformat())
        if task_is_due(task, today):
            people_status = html.unescape(status)
        else:
            people_status = "assigned:\n" + html.unescape(status).replace("⏳ ", "• ")
        lines.append(
            f"\n#{task['task_number']} — {task['name']}\n"
            f"{schedule_description(task)} at {task['reminder_time']}\n{people_status}"
        )
    lines.append("\nuse /deletetask NUMBER to remove a task, e.g: /deletetask 3.")
    await update.message.reply_text("\n".join(lines))


async def delete_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or not context.args[0].isdigit():
        await list_tasks(update, context)
        return
    task_number = int(context.args[0])
    with connect_db() as connection:
        task = connection.execute(
            "SELECT * FROM tasks WHERE chat_id = ? AND task_number = ? AND active = 1",
            (update.effective_chat.id, task_number),
        ).fetchone()
        if task is not None and task["user_id"] == update.effective_user.id:
            connection.execute("UPDATE tasks SET active = 0 WHERE id = ?", (task["id"],))
            connection.execute(
                """
                UPDATE tasks SET task_number = task_number - 1
                WHERE chat_id = ? AND active = 1 AND task_number > ?
                """,
                (update.effective_chat.id, task_number),
            )
    if task is None:
        await update.message.reply_text("i couldn't find that active task.")
    elif task["user_id"] != update.effective_user.id:
        await update.message.reply_text("i'm unable to delete task as you did not create it.")
    else:
        await update.message.reply_text(
            f"task #{task_number} was removed. remaining task numbers have been updated."
        )


async def check_due_tasks(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now(BOT_TIMEZONE)
    today = now.date()
    current_time = now.strftime("%H:%M")
    with connect_db() as connection:
        tasks = connection.execute(
            "SELECT * FROM tasks WHERE active = 1 AND reminder_time <= ?", (current_time,)
        ).fetchall()

    for task in tasks:
        if task["last_reminded_date"] == today.isoformat() or not task_is_due(task, today):
            continue
        status, all_completed = completion_status(task, today.isoformat())
        keyboard = None if all_completed else InlineKeyboardMarkup(
            [[InlineKeyboardButton(
                "✅ mark myself as complete", callback_data=f"complete:{task['id']}:{today.isoformat()}"
            )]]
        )
        try:
            await context.bot.send_message(
                chat_id=task["chat_id"],
                text=(
                    f"⏰ time to complete: <b>{html.escape(task['name'])}</b>\n\n"
                    f"{status}"
                ),
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        except Exception:
            LOGGER.exception("could not send reminder for task %s", task["id"])
            continue
        with connect_db() as connection:
            connection.execute(
                "UPDATE tasks SET last_reminded_date = ? WHERE id = ?",
                (today.isoformat(), task["id"]),
            )


async def complete_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, task_id_text, due_date = query.data.split(":", 2)
    task_id = int(task_id_text)
    with connect_db() as connection:
        task = connection.execute(
            """
            SELECT t.* FROM tasks t
            JOIN task_members tm ON tm.task_id = t.id
            WHERE t.id = ? AND t.chat_id = ? AND tm.user_id = ? AND t.active = 1
            """,
            (task_id, update.effective_chat.id, update.effective_user.id),
        ).fetchone()
        if task is not None:
            connection.execute(
                """
                INSERT OR IGNORE INTO completions (task_id, user_id, due_date, completed_at)
                VALUES (?, ?, ?, ?)
                """,
                (task_id, update.effective_user.id, due_date, datetime.now(BOT_TIMEZONE).isoformat()),
            )
    if task is None:
        await query.answer("you are not assigned to this task :(", show_alert=True)
        return
    await query.answer("your completion was recorded 🎉")
    status, all_completed = completion_status(task, due_date)
    keyboard = None if all_completed else InlineKeyboardMarkup(
        [[InlineKeyboardButton(
            "✅ mark myself as complete", callback_data=f"complete:{task_id}:{due_date}"
        )]]
    )
    await query.edit_message_text(
        f"{'🎉 everyone completed' if all_completed else 'accountability update'}: "
        f"<b>{html.escape(task['name'])}</b>\n\n{status}",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            ("start", "Show help"),
            ("join", "Register as a selectable group member"),
            ("addtask", "Create a recurring task"),
            ("tasks", "List your tasks"),
            ("deletetask", "List tasks or remove one by number"),
            ("cancel", "Cancel task creation"),
        ]
    )
    application.job_queue.run_repeating(check_due_tasks, interval=30, first=1)


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set. See README.md for setup instructions.")

    initialize_db()
    application = Application.builder().token(token).post_init(post_init).build()
    application.add_handler(MessageHandler(filters.ALL, register_message), group=-1)
    application.add_handler(CallbackQueryHandler(register_callback), group=-1)
    application.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("addtask", add_task)],
            states={
                NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_name)],
                SCHEDULE_TYPE: [CallbackQueryHandler(choose_schedule, pattern=r"^schedule:")],
                SCHEDULE_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_schedule_value)],
                REMINDER_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_reminder_time)],
                ASSIGNEES: [CallbackQueryHandler(choose_assignees, pattern=r"^assign:")],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )
    )
    application.add_handler(CallbackQueryHandler(complete_task, pattern=r"^complete:"))
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("join", join))
    application.add_handler(CommandHandler("tasks", list_tasks))
    application.add_handler(CommandHandler("deletetask", delete_task))

    print(f"Bot is running in timezone {TIMEZONE_NAME}. Press Ctrl+C to stop it.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
