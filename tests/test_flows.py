"""Drive the guided /addtask, /edittask and /deletetask handlers step by step with fake updates."""
import asyncio
import itertools
from types import SimpleNamespace

import pytest
from telegram.ext import ConversationHandler

import bot as app
import storage
from conftest import CHAT, A, B, C, make_task
from test_bot import at

END = ConversationHandler.END


class Chat:
    def __init__(self, user_id=A, chat_type="supergroup"):
        self.user_id = user_id
        self.chat = SimpleNamespace(id=CHAT, type=chat_type)
        self.replies = []
        self.context = SimpleNamespace(user_data={}, args=[], bot=None)
        self._message_ids = itertools.count(1)
        self.last_message_id = None  # the bot's latest message, whose buttons a tap presses

    def _update(self, message=None, query=None):
        return SimpleNamespace(
            effective_chat=self.chat, effective_user=SimpleNamespace(id=self.user_id),
            message=message, effective_message=message or (query.message if query else None),
            callback_query=query,
        )

    def send(self, handler, text="", args=()):
        replies = self.replies

        async def reply_text(body, reply_markup=None, **_):
            replies.append((body, reply_markup))
            self.last_message_id = next(self._message_ids)
            return SimpleNamespace(message_id=self.last_message_id)

        message = SimpleNamespace(text=text, reply_text=reply_text)
        self.context.args = list(args)
        return asyncio.run(handler(self._update(message=message), self.context))

    def tap(self, handler, data):
        replies = self.replies
        answers = []

        async def answer(text=None, show_alert=False):
            answers.append(text)

        async def edit_message_text(body, reply_markup=None, **_):
            replies.append((body, reply_markup))

        async def edit_message_reply_markup(reply_markup=None, **_):
            replies.append(("<markup>", reply_markup))

        query = SimpleNamespace(
            data=data, answer=answer, edit_message_text=edit_message_text,
            edit_message_reply_markup=edit_message_reply_markup,
            message=SimpleNamespace(chat=self.chat, message_id=self.last_message_id),
        )
        state = asyncio.run(handler(self._update(query=query), self.context))
        self.answers = answers
        return state

    @property
    def last(self):
        return self.replies[-1][0]


@pytest.fixture(autouse=True)
def fixed_time(monkeypatch):
    monkeypatch.setattr(app, "now", lambda: at("2026-11-17", "10:00"))


def test_create_every_day_task_tc05():
    chat = Chat()
    assert chat.send(app.add_task) == app.ADD_NAME
    assert chat.send(app.add_name, "Daily devo") == app.ADD_SCHEDULE_TYPE
    assert chat.tap(app.on_schedule_type, "schedule:interval") == app.ADD_SCHEDULE_VALUE
    assert chat.send(app.add_schedule_value, "0") == app.ADD_SCHEDULE_VALUE
    assert "e.g: 2" in chat.last
    assert chat.send(app.add_schedule_value, "1") == app.ADD_TIME
    assert "Asia/Singapore" in chat.last
    assert chat.send(app.add_time, "7pm") == app.ADD_TIME
    assert chat.send(app.add_time, "21:00") == app.ADD_PEOPLE
    labels = [row[0].text for row in chat.replies[-1][1].inline_keyboard]
    assert labels == ["✅ Alice", "☐ Bob", "☐ Cara", "Done"]  # creator preselected, joined only
    assert chat.tap(app.add_people, f"assign:{B}") == app.ADD_PEOPLE
    assert chat.tap(app.add_people, "assign:done") == END
    assert chat.last == (
        "✅ task created!\n\ntask #1: Daily devo\n"
        "schedule: every day at 21:00 (Asia/Singapore)\npeople involved: Alice, Bob"
    )
    assert storage.get_task(1)["last_reminded_date"] is None  # 21:00 is still ahead (FR-16)


def test_weekday_task_and_reminder_time_already_passed_tc06_tc22():
    chat = Chat()
    chat.send(app.add_task)
    chat.send(app.add_name, "Gym")
    chat.tap(app.on_schedule_type, "schedule:weekly")
    assert chat.send(app.add_schedule_value, "Mon Wed") == app.ADD_SCHEDULE_VALUE
    assert chat.send(app.add_schedule_value, "Mon, Wed, Fri") == app.ADD_TIME
    chat.send(app.add_time, "07:00")
    chat.tap(app.add_people, f"assign:{C}")
    chat.tap(app.add_people, "assign:done")
    assert "on Monday, Wednesday, Friday at 07:00" in chat.last
    assert storage.get_task(1)["last_reminded_date"] == "2026-11-17"


def test_at_least_one_person_tc11():
    chat = Chat()
    chat.send(app.add_task)
    chat.send(app.add_name, "x")
    chat.tap(app.on_schedule_type, "schedule:interval")
    chat.send(app.add_schedule_value, "1")
    chat.send(app.add_time, "21:00")
    chat.tap(app.add_people, f"assign:{A}")  # deselect the creator
    assert chat.tap(app.add_people, "assign:done") == app.ADD_PEOPLE
    assert chat.answers == ["please select at least one person."]
    assert storage.active_tasks(CHAT) == []


def test_cancel_saves_nothing_tc12():
    chat = Chat()
    chat.send(app.add_task)
    chat.send(app.add_name, "x")
    assert chat.send(app.cancel) == END
    assert "nothing was saved" in chat.last and storage.active_tasks(CHAT) == []


def test_private_chat_is_refused():
    chat = Chat(chat_type="private")
    assert chat.send(app.add_task) == END
    assert "group" in chat.last


def test_edit_name_by_non_creator_tc33():
    make_task()
    chat = Chat(user_id=B)
    assert chat.send(app.edit_task, args=["1"]) == app.EDIT_CHOICE
    assert chat.tap(app.edit_choice, "edit:name") == app.EDIT_NAME
    assert chat.send(app.edit_name, "Evening devo") == END
    assert storage.get_task(1)["name"] == "Evening devo"
    assert "task updated" in chat.last


def test_edit_bad_number_tc46():
    make_task()
    chat = Chat()
    assert chat.send(app.edit_task, args=["99"]) is None  # no flow starts
    assert "couldn't find task 99" in chat.last
    assert chat.send(app.edit_task) is None
    assert "#1 Daily devo" in chat.last  # no number shows the list


def test_edit_cancel_tc45():
    make_task()
    chat = Chat()
    chat.send(app.edit_task, args=["1"])
    chat.tap(app.edit_choice, "edit:time")
    assert chat.send(app.cancel) == END
    assert chat.last == "editing cancelled. nothing was changed."


def test_edit_interval_restart_and_continue_tc44():
    make_task(interval_days=3, today="2026-11-16")
    make_task("second", interval_days=3, today="2026-11-16")
    for number, choice, start in (("1", "cycle:restart", "2026-11-17"), ("2", "cycle:continue", "2026-11-16")):
        chat = Chat()
        chat.send(app.edit_task, args=[number])
        chat.tap(app.edit_choice, "edit:schedule")
        chat.tap(app.on_schedule_type, "schedule:interval")
        assert chat.send(app.edit_schedule_value, "2") == app.EDIT_CYCLE
        assert chat.tap(app.edit_cycle, choice) == END
        task = storage.get_task_by_number(CHAT, int(number))
        assert (task["interval_days"], task["start_date"]) == (2, start)


def test_weekday_to_interval_starts_today_without_asking_tc47():
    make_task(schedule_type="weekly", interval_days=None, weekdays="0", today="2026-11-10")
    chat = Chat()
    chat.send(app.edit_task, args=["1"])
    chat.tap(app.edit_choice, "edit:schedule")
    chat.tap(app.on_schedule_type, "schedule:interval")
    assert chat.send(app.edit_schedule_value, "2") == END
    task = storage.get_task(1)
    assert (task["schedule_type"], task["start_date"], task["weekdays"]) == ("interval", "2026-11-17", None)


def test_edit_schedule_and_time_tc34():
    make_task(schedule_type="weekly", interval_days=None, weekdays="0", today="2026-11-10")
    chat = Chat()
    chat.send(app.edit_task, args=["1"])
    chat.tap(app.edit_choice, "edit:schedule")
    chat.tap(app.on_schedule_type, "schedule:weekly")
    assert chat.send(app.edit_schedule_value, "Tue, Thu") == END
    chat.send(app.edit_task, args=["1"])
    chat.tap(app.edit_choice, "edit:time")
    assert chat.send(app.edit_time, "9pm") == app.EDIT_TIME
    assert chat.send(app.edit_time, "22:00") == END
    assert "on Tuesday, Thursday at 22:00" in chat.last


def test_edit_people_tc35():
    make_task()
    chat = Chat()
    chat.send(app.edit_task, args=["1"])
    chat.tap(app.edit_choice, "edit:people")
    chat.tap(app.edit_people, f"assign:{C}")
    chat.tap(app.edit_people, f"assign:{B}")
    assert chat.tap(app.edit_people, "assign:done") == END
    assert storage.task_member_ids(1) == {A, C}


def test_delete_needs_yes_tc37_tc38_tc39():
    make_task("one")
    make_task("two")
    make_task("three")
    chat = Chat(user_id=B)
    chat.send(app.delete_task, args=["1"])
    yes, no = (row.callback_data for row in chat.replies[-1][1].inline_keyboard[0])
    chat.tap(app.confirm_delete, no)
    assert "nothing was deleted" in chat.last and len(storage.active_tasks(CHAT)) == 3
    chat.tap(app.confirm_delete, yes)
    assert [(t["task_number"], t["name"]) for t in storage.active_tasks(CHAT)] == [(1, "two"), (2, "three")]
    chat.send(app.delete_task, args=["99"])
    assert "couldn't find task 99" in chat.last
    chat.send(app.delete_task)
    assert "#1 two" in chat.last


def test_join_confirms_by_name_tc02():
    chat = Chat(user_id=42)
    chat.context.user_data = {}

    async def go():
        update = chat._update(message=SimpleNamespace(reply_text=None))
        update.effective_user = SimpleNamespace(id=42, full_name="Dan", username="dan")
        replies = []

        async def reply_text(body, **_):
            replies.append(body)

        update.message.reply_text = reply_text
        await app.join(update, chat.context)
        await app.join(update, chat.context)
        return replies

    replies = asyncio.run(go())
    assert replies[0] == "✅ Dan can now be selected for tasks in this chat."
    assert "already joined" in replies[1]
    assert [m["user_id"] for m in storage.list_members(CHAT)].count(42) == 1
