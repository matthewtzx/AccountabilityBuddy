"""AccountabilityBuddy: a Telegram group bot for shared recurring tasks."""
from __future__ import annotations

import functools
import logging
import os
import warnings
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, ChatMigrated, Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.warnings import PTBUserWarning

import storage
from logic import (
    chunk_overview,
    day_from_code,
    parse_interval,
    parse_name,
    parse_time,
    parse_weekdays,
    reminder_passed,
    render_overview,
    render_reminder,
    schedule_description,
    split_text,
    task_is_due,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)  # request URLs contain the bot token
# The guided flows mix text answers and buttons on purpose; per-message tracking does not fit.
warnings.filterwarnings("ignore", message=".*per_message.*", category=PTBUserWarning)
LOGGER = logging.getLogger(__name__)

TIMEZONE_NAME = os.getenv("BOT_TIMEZONE", "Asia/Singapore")
try:
    BOT_TIMEZONE = ZoneInfo(TIMEZONE_NAME)
except ZoneInfoNotFoundError as exc:
    raise RuntimeError(f"Unknown BOT_TIMEZONE: {TIMEZONE_NAME}") from exc

(
    ADD_NAME, ADD_SCHEDULE_TYPE, ADD_SCHEDULE_VALUE, ADD_TIME, ADD_PEOPLE,
    EDIT_CHOICE, EDIT_NAME, EDIT_SCHEDULE_TYPE, EDIT_SCHEDULE_VALUE, EDIT_CYCLE, EDIT_TIME, EDIT_PEOPLE,
) = range(12)
FLOW_TIMEOUT_SECONDS = 15 * 60

NAME_PROMPT = "what is the task name? e.g: Daily devo\n\nsend /cancel to stop."
NAME_ERROR = "please send a task name of 1 to 100 characters, e.g: Daily devo"
INTERVAL_PROMPT = "how many days between each time? send a whole number, e.g: 1 for every day or 2 for every other day."
INTERVAL_ERROR = "please send a whole number of 1 or more, e.g: 2"
WEEKDAY_PROMPT = "which days? separate full or short names with commas, e.g: Mon, Wed, Fri"
WEEKDAY_ERROR = "sorry, i couldn't understand those days. separate full or short names with commas, e.g: Mon, Wed, Fri"
TIME_ERROR = f"please use 24-hour HH:MM in {TIMEZONE_NAME} time, e.g: 07:30 or 21:00"
PRIVATE_TEXT = "i only work in group chats. add me to a group, then send /join there."
STALE_BUTTON_TEXT = "only the person who started this can answer it, or it has expired."


def now() -> datetime:
    return datetime.now(BOT_TIMEZONE)


def time_prompt() -> str:
    return f"what time should the reminder be sent? use 24-hour HH:MM in {TIMEZONE_NAME} time, e.g: 21:00"


def schedule_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("every N days", callback_data="schedule:interval"),
        InlineKeyboardButton("specific days of the week", callback_data="schedule:weekly"),
    ]])


def people_keyboard(chat_id: int, selected: set[int]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            f"{'✅' if member['user_id'] in selected else '☐'} {member['display_name']}"[:64],
            callback_data=f"assign:{member['user_id']}",
        )]
        for member in storage.list_members(chat_id)
    ]
    rows.append([InlineKeyboardButton("Done", callback_data="assign:done")])
    return InlineKeyboardMarkup(rows)


PEOPLE_PROMPT = (
    "who is involved? tap names to select or deselect them, then tap Done.\n\n"
    "if someone is missing, send /cancel and ask them to send /join first."
)


def describe_task(task) -> str:
    names = ", ".join(person.name for person in storage.task_view(task, "").people)
    return (
        f"task #{task['task_number']}: {task['name']}\n"
        f"schedule: {schedule_description(task)} at {task['reminder_time']} ({TIMEZONE_NAME})\n"
        f"people involved: {names}"
    )


def group_only(handler):
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type == ChatType.PRIVATE:
            await update.effective_message.reply_text(PRIVATE_TEXT)
            return ConversationHandler.END
        return await handler(update, context)
    return wrapper


def drafts(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.setdefault("drafts", {})


def get_draft(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[dict]:
    return drafts(context).get(update.effective_chat.id)


async def reply(update: Update, text: str, reply_markup=None) -> None:
    """Answer in place when the update is a button tap, otherwise reply to the message."""
    if update.callback_query is not None:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=reply_markup)


# ---------------------------------------------------------------- basic commands

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "hi! i'm AccountabilityBuddy. i remind your group to do shared recurring tasks, "
        "and each person ticks off their own name.\n\n"
        "1. everyone sends /join once so they can be picked for tasks\n"
        "2. /addtask creates a task\n"
        "3. /tasks lists this chat's tasks and today's status\n"
        "4. /edittask NUMBER changes one, /deletetask NUMBER removes one\n\n"
        f"at 12am on a day a task is due i post an overview, and at the reminder time i post a reminder. "
        f"all times are {TIMEZONE_NAME}."
    )
    if update.effective_chat.type == ChatType.PRIVATE:
        text += "\n\n" + PRIVATE_TEXT
    await update.message.reply_text(text)


@group_only
async def join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat_id = update.effective_chat.id
    already = any(member["user_id"] == user.id for member in storage.list_members(chat_id))
    storage.join_member(chat_id, user.id, user.full_name, user.username)
    if already:
        await update.message.reply_text(f"{user.full_name}, you've already joined. you can be selected for tasks here.")
    else:
        await update.message.reply_text(f"✅ {user.full_name} can now be selected for tasks in this chat.")


async def refresh_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat, user = update.effective_chat, update.effective_user
    if chat is None or user is None or chat.type == ChatType.PRIVATE:
        return
    storage.refresh_member(chat.id, user.id, user.full_name, user.username)


async def send_task_list(update: Update) -> None:
    tasks = storage.active_tasks(update.effective_chat.id)
    if not tasks:
        await update.effective_message.reply_text("there are no tasks in this chat yet. use /addtask to create one.")
        return
    today = now().date()
    lines = [f"tasks in this chat (times are {TIMEZONE_NAME}):"]
    for task in tasks:
        view = storage.task_view(task, today.isoformat())
        if task_is_due(task, today):
            status = " · ".join(f"{'✅' if p.done else '⏳'} {p.name}" for p in view.people)
        else:
            status = "not due"
        lines += [
            "",
            f"#{task['task_number']} {task['name']}",
            f"{schedule_description(task)} at {task['reminder_time']}",
            f"people: {', '.join(p.name for p in view.people)}",
            f"today: {status}",
        ]
    lines += ["", "use /edittask NUMBER to change a task or /deletetask NUMBER to remove one."]
    for piece in split_text("\n".join(lines)):
        await update.effective_message.reply_text(piece)


@group_only
async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_task_list(update)


async def find_task_from_args(update: Update, context: ContextTypes.DEFAULT_TYPE, command: str):
    """Return the task named by the command's number, or None after replying."""
    if not context.args:
        await send_task_list(update)
        return None
    text = context.args[0].lstrip("#")
    task = storage.get_task_by_number(update.effective_chat.id, int(text)) if text.isdigit() else None
    if task is None:
        await update.message.reply_text(
            f"i couldn't find task {context.args[0]} in this chat. send /tasks to see the numbers, "
            f"then e.g: /{command} 1"
        )
    return task


# ---------------------------------------------------------------- shared flow steps

def apply_schedule_text(draft: dict, text: str) -> Optional[str]:
    """Store the schedule answer in the draft. Returns an error message, or None if valid."""
    if draft["schedule_type"] == "interval":
        interval = parse_interval(text)
        if interval is None:
            return INTERVAL_ERROR
        draft["interval_days"], draft["weekdays"] = interval, None
    else:
        weekdays = parse_weekdays(text)
        if weekdays is None:
            return WEEKDAY_ERROR
        draft["interval_days"], draft["weekdays"] = None, weekdays
    return None


async def on_schedule_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    draft = get_draft(update, context)
    draft["schedule_type"] = query.data.split(":", 1)[1]
    await query.edit_message_text(INTERVAL_PROMPT if draft["schedule_type"] == "interval" else WEEKDAY_PROMPT)
    return ADD_SCHEDULE_VALUE if draft["mode"] == "add" else EDIT_SCHEDULE_VALUE


async def on_person_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[set[int]]:
    """Handle a tap in the people picker. Returns the selection when Done was tapped and valid."""
    query = update.callback_query
    draft = get_draft(update, context)
    choice = query.data.split(":", 1)[1]
    if choice != "done":
        draft["assignees"] ^= {int(choice)}
        await query.answer()
        await query.edit_message_reply_markup(people_keyboard(update.effective_chat.id, draft["assignees"]))
        return None
    if not draft["assignees"]:
        await query.answer("please select at least one person.", show_alert=True)
        return None
    await query.answer()
    return set(draft["assignees"])


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    draft = drafts(context).pop(update.effective_chat.id, None)
    if draft and draft["mode"] == "edit":
        await update.message.reply_text("editing cancelled. nothing was changed.")
    else:
        await update.message.reply_text("task creation cancelled. nothing was saved.")
    return ConversationHandler.END


async def nothing_to_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("there is nothing to cancel.")


async def on_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    draft = drafts(context).pop(update.effective_chat.id, None)
    action = "editing" if draft and draft["mode"] == "edit" else "task creation"
    await context.bot.send_message(update.effective_chat.id, f"{action} timed out, so nothing was saved.")


async def stale_flow_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer(STALE_BUTTON_TEXT, show_alert=True)


# ---------------------------------------------------------------- /addtask

@group_only
async def add_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id, user_id = update.effective_chat.id, update.effective_user.id
    members = {member["user_id"] for member in storage.list_members(chat_id)}
    if not members:
        await update.message.reply_text("nobody has joined yet. everyone involved should send /join first.")
        return ConversationHandler.END
    drafts(context)[chat_id] = {"mode": "add", "assignees": {user_id} & members}
    await update.message.reply_text(NAME_PROMPT)
    return ADD_NAME


async def add_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = parse_name(update.message.text)
    if name is None:
        await update.message.reply_text(NAME_ERROR)
        return ADD_NAME
    get_draft(update, context)["name"] = name
    await update.message.reply_text("how often should it be done?", reply_markup=schedule_keyboard())
    return ADD_SCHEDULE_TYPE


async def add_schedule_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    error = apply_schedule_text(get_draft(update, context), update.message.text)
    if error:
        await update.message.reply_text(error)
        return ADD_SCHEDULE_VALUE
    await update.message.reply_text(time_prompt())
    return ADD_TIME


async def add_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    reminder_time = parse_time(update.message.text)
    if reminder_time is None:
        await update.message.reply_text(TIME_ERROR)
        return ADD_TIME
    draft = get_draft(update, context)
    draft["reminder_time"] = reminder_time
    await update.message.reply_text(
        PEOPLE_PROMPT, reply_markup=people_keyboard(update.effective_chat.id, draft["assignees"])
    )
    return ADD_PEOPLE


async def add_people(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assignees = await on_person_toggle(update, context)
    if assignees is None:
        return ADD_PEOPLE
    draft = drafts(context).pop(update.effective_chat.id)
    current = now()
    task = storage.create_task(
        chat_id=update.effective_chat.id,
        creator_id=update.effective_user.id,
        name=draft["name"],
        schedule_type=draft["schedule_type"],
        interval_days=draft["interval_days"],
        weekdays=draft["weekdays"],
        reminder_time=draft["reminder_time"],
        today=current.date().isoformat(),
        assignees=assignees,
        # FR-16: if today's reminder time has already passed, the first reminder is the next due day.
        skip_today_reminder=reminder_passed(current, draft["reminder_time"]),
    )
    await update.callback_query.edit_message_text("✅ task created!\n\n" + describe_task(task))
    return ConversationHandler.END


# ---------------------------------------------------------------- /edittask

@group_only
async def edit_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    task = await find_task_from_args(update, context, "edittask")
    if task is None:
        return ConversationHandler.END
    drafts(context)[update.effective_chat.id] = {"mode": "edit", "task_id": task["id"]}
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("name", callback_data="edit:name"),
         InlineKeyboardButton("schedule", callback_data="edit:schedule")],
        [InlineKeyboardButton("reminder time", callback_data="edit:time"),
         InlineKeyboardButton("people involved", callback_data="edit:people")],
    ])
    await update.message.reply_text(
        f"what would you like to change?\n\n{describe_task(task)}\n\nsend /cancel to stop.",
        reply_markup=keyboard,
    )
    return EDIT_CHOICE


async def edit_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    draft = get_draft(update, context)
    choice = query.data.split(":", 1)[1]
    if choice == "name":
        await query.edit_message_text(NAME_PROMPT.replace("what is the task name?", "what is the new name?"))
        return EDIT_NAME
    if choice == "schedule":
        await query.edit_message_text("how often should it be done?", reply_markup=schedule_keyboard())
        return EDIT_SCHEDULE_TYPE
    if choice == "time":
        await query.edit_message_text(time_prompt())
        return EDIT_TIME
    draft["assignees"] = storage.task_member_ids(draft["task_id"])
    await query.edit_message_text(
        PEOPLE_PROMPT, reply_markup=people_keyboard(update.effective_chat.id, draft["assignees"])
    )
    return EDIT_PEOPLE


async def edit_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = parse_name(update.message.text)
    if name is None:
        await update.message.reply_text(NAME_ERROR)
        return EDIT_NAME
    return await save_edit(update, context, {"name": name})


async def edit_schedule_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    draft = get_draft(update, context)
    error = apply_schedule_text(draft, update.message.text)
    if error:
        await update.message.reply_text(error)
        return EDIT_SCHEDULE_VALUE
    fields = {
        "schedule_type": draft["schedule_type"],
        "interval_days": draft["interval_days"],
        "weekdays": draft["weekdays"],
    }
    task = storage.get_task(draft["task_id"])
    if draft["schedule_type"] == "interval":
        if task is not None and task["schedule_type"] == "interval":
            draft["fields"] = fields
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("restart the cycle from today", callback_data="cycle:restart")],
                [InlineKeyboardButton("continue the existing cycle", callback_data="cycle:continue")],
            ])
            await update.message.reply_text(
                f"should the cycle restart from today, or continue from when it started "
                f"({task['start_date']})?",
                reply_markup=keyboard,
            )
            return EDIT_CYCLE
        # A weekday task has no cycle to continue, so the new one starts today.
        fields["start_date"] = now().date().isoformat()
    return await save_edit(update, context, fields)


async def edit_cycle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    fields = dict(get_draft(update, context)["fields"])
    if query.data == "cycle:restart":
        fields["start_date"] = now().date().isoformat()
    return await save_edit(update, context, fields)


async def edit_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    reminder_time = parse_time(update.message.text)
    if reminder_time is None:
        await update.message.reply_text(TIME_ERROR)
        return EDIT_TIME
    return await save_edit(update, context, {"reminder_time": reminder_time})


async def edit_people(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assignees = await on_person_toggle(update, context)
    if assignees is None:
        return EDIT_PEOPLE
    return await save_edit(update, context, {}, assignees)


def edit_fields_with_reminder_rule(task, fields: dict, current: datetime) -> dict:
    """FR-16 applied to edits: a changed time or schedule only fires today if its time is still ahead."""
    if not {"reminder_time", "schedule_type", "start_date"} & fields.keys():
        return fields
    today = current.date().isoformat()
    new_time = fields.get("reminder_time", task["reminder_time"])
    if task["last_reminded_date"] != today and reminder_passed(current, new_time):
        return {**fields, "last_reminded_date": today}
    return fields


async def save_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, fields: dict,
                    assignees: Optional[set[int]] = None) -> int:
    draft = drafts(context).pop(update.effective_chat.id)
    task = storage.get_task(draft["task_id"])
    if task is None or not task["active"]:
        await reply(update, "that task was deleted while you were editing it. nothing was changed.")
        return ConversationHandler.END
    storage.update_task(task["id"], edit_fields_with_reminder_rule(task, fields, now()), assignees)
    await reply(update, "✏️ task updated!\n\n" + describe_task(storage.get_task(task["id"])))
    return ConversationHandler.END


# ---------------------------------------------------------------- /deletetask

@group_only
async def delete_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    task = await find_task_from_args(update, context, "deletetask")
    if task is None:
        return
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Yes, delete", callback_data=f"delete:yes:{task['id']}"),
        InlineKeyboardButton("No", callback_data=f"delete:no:{task['id']}"),
    ]])
    await update.message.reply_text(
        f"delete task #{task['task_number']} ({task['name']})? its past completions are kept.",
        reply_markup=keyboard,
    )


async def confirm_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, answer, task_id = query.data.split(":")
    task = storage.get_task(int(task_id))
    await query.answer()
    if task is None or not task["active"] or task["chat_id"] != update.effective_chat.id:
        await query.edit_message_text("that task has already been deleted.")
    elif answer == "no":
        await query.edit_message_text(f"nothing was deleted. task #{task['task_number']} ({task['name']}) is kept.")
    else:
        storage.delete_task(task["id"])
        await query.edit_message_text(
            f"🗑 task #{task['task_number']} ({task['name']}) was deleted. "
            "the remaining tasks have been renumbered; send /tasks to see them."
        )


# ---------------------------------------------------------------- ticking

async def redraw_messages(bot: Bot, task_id: int, day: str) -> None:
    """FR-22: every overview and reminder showing this task for this day shows the latest ticks."""
    for row in storage.messages_for(task_id, day):
        if row["kind"] == "reminder":
            text, keyboard = render_reminder(storage.task_view(storage.get_task(task_id), day), day)
        else:
            part, parts = (int(value) for value in row["kind"].split(" ", 1)[1].split("/"))
            views = [storage.task_view(task, day)
                     for task in storage.tasks_in_message(row["chat_id"], row["message_id"])]
            text, keyboard = render_overview(views, day, TIMEZONE_NAME, part, parts)
        try:
            await bot.edit_message_text(
                text, chat_id=row["chat_id"], message_id=row["message_id"],
                parse_mode=ParseMode.HTML, reply_markup=keyboard,
            )
        except BadRequest as error:
            if "not modified" not in str(error).lower():
                LOGGER.warning("could not update message %s: %s", row["message_id"], error)
        except TelegramError:
            LOGGER.exception("could not update message %s", row["message_id"])


async def announce_completion(bot: Bot, task, user_id: int, day: str) -> None:
    view = storage.task_view(task, day)
    name = next((p.name for p in view.people if p.user_id == user_id), "someone")
    suffix = ""
    if day != now().date().isoformat():
        suffix = f" for {date.fromisoformat(day).strftime('%a %d %b')}"
    text = f"✅ {name} has completed {task['name']}{suffix}"
    if view.everyone_done and len(view.people) > 1:
        text += f"\n🎉 everyone has completed {task['name']}{suffix}!"
    await bot.send_message(task["chat_id"], text)


async def check_tick_allowed(update: Update, task_id: int, owner_id: Optional[int]):
    """FR-18. Returns the task when the tap may change it; otherwise answers the tap and returns None."""
    query = update.callback_query
    task = storage.get_task(task_id)
    chat_id = query.message.chat.id if query.message else None
    if task is None or not task["active"] or task["chat_id"] != chat_id:
        await query.answer("this task has been deleted.", show_alert=True)
        return None
    if update.effective_user.id not in storage.task_member_ids(task_id):
        await query.answer("you are not involved in this task.", show_alert=True)
        return None
    if owner_id is not None and owner_id != update.effective_user.id:
        await query.answer("you can only tick your own name.", show_alert=True)
        return None
    return task


async def on_tick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, task_id, code, owner_id = query.data.split(":")
    task = await check_tick_allowed(update, int(task_id), int(owner_id))
    if task is None:
        return
    day = day_from_code(code)
    ticked = storage.toggle_completion(task["id"], update.effective_user.id, day, now().isoformat())
    await query.answer("ticked 🎉" if ticked else "unticked")
    if ticked:  # FR-19: an untick posts nothing.
        await announce_completion(context.bot, task, update.effective_user.id, day)
    await redraw_messages(context.bot, task["id"], day)


async def on_legacy_complete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Buttons on reminders sent before v1 still record a tick."""
    query = update.callback_query
    _, task_id, day = query.data.split(":", 2)
    task = await check_tick_allowed(update, int(task_id), None)
    if task is None:
        return
    ticked = storage.tick(task["id"], update.effective_user.id, day, now().isoformat())
    await query.answer("ticked 🎉" if ticked else "you've already ticked this one.")
    if ticked:
        await announce_completion(context.bot, task, update.effective_user.id, day)
    storage.record_message(query.message.chat.id, query.message.message_id, [task["id"]], "reminder", day)
    await redraw_messages(context.bot, task["id"], day)


# ---------------------------------------------------------------- scheduled posts

async def handle_send_error(error: TelegramError, chat_id: int) -> bool:
    """Returns True when the failure is permanent, so the post should not be retried."""
    if isinstance(error, ChatMigrated):
        storage.migrate_chat(chat_id, error.new_chat_id)
        LOGGER.info("chat %s moved to %s", chat_id, error.new_chat_id)
        return False
    if isinstance(error, Forbidden):
        LOGGER.warning("cannot post in chat %s: %s", chat_id, error)
        return True
    LOGGER.exception("could not post in chat %s", chat_id, exc_info=error)
    return False


async def run_overviews(bot: Bot, current: datetime) -> None:
    """FR-10 to FR-13 and FR-16: one overview per chat per day, also as a catch-up after downtime."""
    today = current.date()
    iso = today.isoformat()
    by_chat: dict[int, list] = {}
    for task in storage.active_tasks():
        by_chat.setdefault(task["chat_id"], []).append(task)
    for chat_id, tasks in by_chat.items():
        if storage.overview_done(chat_id, iso):
            continue
        due = [t for t in tasks if task_is_due(t, today) and t["created_date"] != iso]
        chunks = chunk_overview([storage.task_view(t, iso) for t in due])
        try:
            for number, chunk in enumerate(chunks, start=1):
                text, keyboard = render_overview(chunk, iso, TIMEZONE_NAME, number, len(chunks))
                message = await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
                storage.record_message(chat_id, message.message_id, [v.task_id for v in chunk],
                                       f"overview {number}/{len(chunks)}", iso)
        except TelegramError as error:
            if not await handle_send_error(error, chat_id):
                continue
        storage.mark_overview_done(chat_id, iso)
        for task in due:
            storage.log_due(task["id"], iso)


async def run_reminders(bot: Bot, current: datetime) -> None:
    """FR-14 to FR-16: one reminder per task per due day, none if everyone has ticked."""
    today = current.date()
    iso = today.isoformat()
    for task in storage.active_tasks():
        if (task["last_reminded_date"] == iso or not task_is_due(task, today)
                or not reminder_passed(current, task["reminder_time"])):
            continue
        view = storage.task_view(task, iso)
        if not view.everyone_done:
            text, keyboard = render_reminder(view, iso)
            try:
                message = await bot.send_message(
                    task["chat_id"], text, parse_mode=ParseMode.HTML, reply_markup=keyboard
                )
            except TelegramError as error:
                if not await handle_send_error(error, task["chat_id"]):
                    continue
            else:
                storage.record_message(task["chat_id"], message.message_id, [task["id"]], "reminder", iso)
        storage.mark_reminded(task["id"], iso)
        storage.log_due(task["id"], iso)


async def scheduler_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    current = now()
    await run_overviews(context.bot, current)
    await run_reminders(context.bot, current)


# ---------------------------------------------------------------- wiring

async def on_migrate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message.migrate_to_chat_id:
        storage.migrate_chat(message.chat.id, message.migrate_to_chat_id)
        LOGGER.info("chat %s upgraded to %s", message.chat.id, message.migrate_to_chat_id)


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands([
        ("start", "Show help"),
        ("join", "Let people pick you for tasks in this chat"),
        ("addtask", "Create a recurring task"),
        ("tasks", "List tasks and today's status"),
        ("edittask", "Change a task: /edittask NUMBER"),
        ("deletetask", "Delete a task: /deletetask NUMBER"),
        ("cancel", "Stop creating or editing a task"),
    ])
    application.job_queue.run_repeating(scheduler_tick, interval=30, first=1)


def build_application(token: str) -> Application:
    application = Application.builder().token(token).post_init(post_init).build()
    text = filters.TEXT & ~filters.COMMAND
    timeout = {ConversationHandler.TIMEOUT: [TypeHandler(Update, on_timeout)]}
    cancel_handler = [CommandHandler("cancel", cancel)]

    application.add_handler(MessageHandler(filters.ALL, refresh_member), group=-1)
    application.add_handler(CallbackQueryHandler(refresh_member), group=-1)

    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler("addtask", add_task)],
        states={
            ADD_NAME: [MessageHandler(text, add_name)],
            ADD_SCHEDULE_TYPE: [CallbackQueryHandler(on_schedule_type, pattern=r"^schedule:")],
            ADD_SCHEDULE_VALUE: [MessageHandler(text, add_schedule_value)],
            ADD_TIME: [MessageHandler(text, add_time)],
            ADD_PEOPLE: [CallbackQueryHandler(add_people, pattern=r"^assign:")],
            **timeout,
        },
        fallbacks=cancel_handler,
        allow_reentry=True,
        conversation_timeout=FLOW_TIMEOUT_SECONDS,
    ))
    application.add_handler(ConversationHandler(
        entry_points=[CommandHandler("edittask", edit_task)],
        states={
            EDIT_CHOICE: [CallbackQueryHandler(edit_choice, pattern=r"^edit:")],
            EDIT_NAME: [MessageHandler(text, edit_name)],
            EDIT_SCHEDULE_TYPE: [CallbackQueryHandler(on_schedule_type, pattern=r"^schedule:")],
            EDIT_SCHEDULE_VALUE: [MessageHandler(text, edit_schedule_value)],
            EDIT_CYCLE: [CallbackQueryHandler(edit_cycle, pattern=r"^cycle:")],
            EDIT_TIME: [MessageHandler(text, edit_time)],
            EDIT_PEOPLE: [CallbackQueryHandler(edit_people, pattern=r"^assign:")],
            **timeout,
        },
        fallbacks=cancel_handler,
        allow_reentry=True,
        conversation_timeout=FLOW_TIMEOUT_SECONDS,
    ))
    application.add_handler(CallbackQueryHandler(on_tick, pattern=r"^t:"))
    application.add_handler(CallbackQueryHandler(on_legacy_complete, pattern=r"^complete:"))
    application.add_handler(CallbackQueryHandler(confirm_delete, pattern=r"^delete:"))
    application.add_handler(CallbackQueryHandler(stale_flow_button, pattern=r"^(assign|schedule|edit|cycle):"))
    application.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, on_migrate))
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("join", join))
    application.add_handler(CommandHandler("tasks", list_tasks))
    application.add_handler(CommandHandler("deletetask", delete_task))
    application.add_handler(CommandHandler("cancel", nothing_to_cancel))
    return application


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set. See README.md for setup instructions.")
    storage.initialize_db()
    print(f"Bot is running in timezone {TIMEZONE_NAME}. Press Ctrl+C to stop it.")
    build_application(token).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
