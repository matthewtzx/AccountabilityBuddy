import asyncio
from datetime import datetime
from types import SimpleNamespace

from telegram.error import Forbidden, NetworkError

import bot as app
import storage
from conftest import CHAT, A, B, C, make_task

TZ = app.BOT_TIMEZONE
DAY1 = "2026-11-17"


def at(day, hhmm):
    y, m, d = (int(x) for x in day.split("-"))
    h, mi = (int(x) for x in hhmm.split(":"))
    return datetime(y, m, d, h, mi, 5, tzinfo=TZ)


def run(coro):
    return asyncio.run(coro)


def tick_all(bot, when):
    run(app.run_overviews(bot, when))
    run(app.run_reminders(bot, when))


def overviews(bot):
    return [m for m in bot.sent if "today's tasks" in m.text]


def reminders(bot):
    return [m for m in bot.sent if m.text.startswith("⏰")]


def test_application_wiring_builds():
    application = app.build_application("123456:TEST-TOKEN")
    assert len(application.handlers[0]) >= 10 and len(application.handlers[-1]) == 2


# ---------------------------------------------------------------- overview

def test_overview_at_midnight_lists_only_due_tasks_tc14_tc15(bot):
    make_task("Daily devo", today="2026-11-16")
    make_task("Gym", schedule_type="weekly", interval_days=None, weekdays="0", today="2026-11-16")  # Mondays
    tick_all(bot, at(DAY1, "00:00"))  # 17 Nov is a Tuesday
    assert len(overviews(bot)) == 1
    message = overviews(bot)[0]
    assert "Daily devo" in message.text and "Gym" not in message.text and "21:00" in message.text
    labels = [row[0].text for row in message.reply_markup.inline_keyboard]
    assert labels == ["☐ Alice", "☐ Bob"]


def test_no_overview_when_nothing_due_tc16(bot):
    make_task("Gym", schedule_type="weekly", interval_days=None, weekdays="0", today="2026-11-16")
    tick_all(bot, at(DAY1, "00:00"))
    assert bot.sent == []
    assert storage.overview_done(CHAT, DAY1)


def test_catch_up_overview_posts_once_tc17(bot):
    make_task(today="2026-11-16")
    tick_all(bot, at(DAY1, "00:10"))
    tick_all(bot, at(DAY1, "00:11"))  # a second start the same day
    assert len(overviews(bot)) == 1


def test_task_created_today_is_not_in_overview_tc21_tc48(bot):
    make_task("Old", today="2026-11-16")
    make_task("New today", today=DAY1)
    tick_all(bot, at(DAY1, "10:00"))  # catch-up after a restart mid-morning
    assert len(overviews(bot)) == 1
    assert "New today" not in overviews(bot)[0].text


def test_failed_overview_is_retried(bot):
    make_task(today="2026-11-16")
    bot.fail_with = NetworkError("down")
    run(app.run_overviews(bot, at(DAY1, "00:00")))
    assert not storage.overview_done(CHAT, DAY1)
    bot.fail_with = None
    run(app.run_overviews(bot, at(DAY1, "00:00")))
    assert len(overviews(bot)) == 1


def test_bot_removed_from_group_stops_retrying(bot):
    make_task(today="2026-11-16")
    bot.fail_with = Forbidden("bot was kicked")
    tick_all(bot, at(DAY1, "21:00"))
    assert storage.overview_done(CHAT, DAY1)
    assert storage.get_task(1)["last_reminded_date"] == DAY1


# ---------------------------------------------------------------- reminders

def test_reminder_at_time_and_only_once_tc18_tc19(bot):
    make_task(today="2026-11-16")
    tick_all(bot, at(DAY1, "20:59"))
    assert reminders(bot) == []
    tick_all(bot, at(DAY1, "21:00"))
    tick_all(bot, at(DAY1, "21:05"))
    assert len(reminders(bot)) == 1
    assert "⏳ Alice" in reminders(bot)[0].text


def test_no_reminder_when_everyone_ticked_tc20(bot):
    task = make_task(today="2026-11-16")
    storage.toggle_completion(task["id"], A, DAY1, "t")
    storage.toggle_completion(task["id"], B, DAY1, "t")
    tick_all(bot, at(DAY1, "21:00"))
    assert reminders(bot) == []


def test_late_reminder_after_downtime_same_day_tc51(bot):
    make_task(today="2026-11-16")
    tick_all(bot, at(DAY1, "23:30"))
    assert len(reminders(bot)) == 1


def test_task_created_after_reminder_time_waits_tc22(bot):
    make_task(today=DAY1, reminder_time="09:00", skip=True)
    tick_all(bot, at(DAY1, "10:02"))
    assert bot.sent == []
    tick_all(bot, at("2026-11-18", "00:00"))
    assert len(overviews(bot)) == 1
    tick_all(bot, at("2026-11-18", "09:00"))
    assert len(reminders(bot)) == 1


def test_every_two_days_tc23(bot):
    make_task(interval_days=2, today=DAY1, reminder_time="09:00", skip=True)
    for day in ("2026-11-18", "2026-11-19"):
        tick_all(bot, at(day, "00:00"))
        tick_all(bot, at(day, "09:00"))
    assert len(overviews(bot)) == 1 and len(reminders(bot)) == 1
    assert "Thursday" in overviews(bot)[0].text


def test_due_days_are_logged_for_o1_o2(bot):
    task = make_task(today="2026-11-16")
    tick_all(bot, at(DAY1, "21:00"))
    with storage.connect() as connection:
        rows = connection.execute("SELECT due_date FROM due_log WHERE task_id = ?", (task["id"],)).fetchall()
    assert [r["due_date"] for r in rows] == [DAY1]


# ---------------------------------------------------------------- edit reminder rule

def test_edited_time_in_past_waits_until_next_due_day(bot):
    task = make_task(today="2026-11-16", reminder_time="23:00")
    fields = app.edit_fields_with_reminder_rule(task, {"reminder_time": "08:00"}, at(DAY1, "10:00"))
    assert fields["last_reminded_date"] == DAY1
    fields = app.edit_fields_with_reminder_rule(task, {"reminder_time": "12:00"}, at(DAY1, "10:00"))
    assert "last_reminded_date" not in fields
    assert app.edit_fields_with_reminder_rule(task, {"name": "x"}, at(DAY1, "10:00")) == {"name": "x"}


# ---------------------------------------------------------------- ticking

class Query:
    def __init__(self, data, chat_id=CHAT, message_id=1):
        self.data = data
        self.message = SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=message_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append(text)


def tap(bot, user_id, data):
    query = Query(data)
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=user_id))
    run(app.on_tick(update, SimpleNamespace(bot=bot)))
    return query


def posted_texts(bot):
    return [m.text for m in bot.sent]


def setup_day(bot):
    task = make_task(today="2026-11-16")
    tick_all(bot, at(DAY1, "00:00"))
    tick_all(bot, at(DAY1, "21:00"))
    bot.sent.clear()
    return task


def test_tick_updates_overview_and_reminder_and_announces_tc25_tc29_tc31(bot, monkeypatch):
    monkeypatch.setattr(app, "now", lambda: at(DAY1, "21:01"))
    task = setup_day(bot)
    tap(bot, A, f"t:{task['id']}:20261117:{A}")
    assert posted_texts(bot) == ["✅ Alice has completed Daily devo"]
    assert len(bot.edited) == 2  # the overview and the reminder
    for edit in bot.edited:
        labels = [row[0].text for row in edit.reply_markup.inline_keyboard]
        assert labels == ["✅ Alice", "☐ Bob"]


def test_last_tick_announces_everyone_tc30(bot, monkeypatch):
    monkeypatch.setattr(app, "now", lambda: at(DAY1, "21:01"))
    task = setup_day(bot)
    tap(bot, A, f"t:{task['id']}:20261117:{A}")
    tap(bot, B, f"t:{task['id']}:20261117:{B}")
    assert posted_texts(bot)[-1] == "✅ Bob has completed Daily devo\n🎉 everyone has completed Daily devo!"


def test_single_person_task_has_no_everyone_message_tc55(bot, monkeypatch):
    monkeypatch.setattr(app, "now", lambda: at(DAY1, "21:01"))
    task = make_task(today="2026-11-16", assignees=(A,))
    tap(bot, A, f"t:{task['id']}:20261117:{A}")
    assert posted_texts(bot) == ["✅ Alice has completed Daily devo"]


def test_cannot_tick_someone_else_tc26(bot):
    task = setup_day(bot)
    query = tap(bot, A, f"t:{task['id']}:20261117:{B}")
    assert query.answers == ["you can only tick your own name."]
    assert bot.sent == [] and not any(p.done for p in storage.task_view(task, DAY1).people)


def test_person_not_involved_cannot_tick_tc27(bot):
    task = setup_day(bot)
    query = tap(bot, C, f"t:{task['id']}:20261117:{A}")
    assert query.answers == ["you are not involved in this task."]


def test_untick_posts_nothing_tc28(bot, monkeypatch):
    monkeypatch.setattr(app, "now", lambda: at(DAY1, "21:01"))
    task = setup_day(bot)
    tap(bot, A, f"t:{task['id']}:20261117:{A}")
    bot.sent.clear()
    tap(bot, A, f"t:{task['id']}:20261117:{A}")
    assert bot.sent == []
    assert not any(p.done for p in storage.task_view(task, DAY1).people)


def test_tick_after_delete_tc50(bot):
    task = setup_day(bot)
    storage.delete_task(task["id"])
    query = tap(bot, A, f"t:{task['id']}:20261117:{A}")
    assert query.answers == ["this task has been deleted."]


def test_tick_on_yesterday_counts_for_yesterday_tc52(bot, monkeypatch):
    task = setup_day(bot)
    monkeypatch.setattr(app, "now", lambda: at("2026-11-18", "00:20"))
    tap(bot, A, f"t:{task['id']}:20261117:{A}")
    assert storage.task_view(task, DAY1).people[0].done
    assert not storage.task_view(task, "2026-11-18").people[0].done
    assert posted_texts(bot) == ["✅ Alice has completed Daily devo for Tue 17 Nov"]


def test_tick_after_edit_redraws_with_current_details_tc49(bot, monkeypatch):
    monkeypatch.setattr(app, "now", lambda: at(DAY1, "21:01"))
    task = setup_day(bot)
    storage.update_task(task["id"], {"name": "Evening devo"})
    tap(bot, A, f"t:{task['id']}:20261117:{A}")
    assert all("Evening devo" in edit.text for edit in bot.edited)
