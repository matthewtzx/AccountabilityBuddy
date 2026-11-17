from datetime import date, datetime

from logic import (
    MAX_MESSAGE_CHARS,
    Person,
    TaskView,
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


def test_interval_inputs_tc08():
    assert parse_interval("0") is None
    assert parse_interval("two") is None
    assert parse_interval("1.5") is None
    assert parse_interval("-1") is None
    assert parse_interval(" 2 ") == 2


def test_weekday_inputs_tc07():
    assert parse_weekdays("Mon, Funday") is None
    assert parse_weekdays("Mon Wed") is None
    assert parse_weekdays("monday, FRIDAY") == "0,4"
    assert parse_weekdays("Mon, Wed, Fri") == "0,2,4"
    assert parse_weekdays("fri, mon, mon") == "0,4"
    assert parse_weekdays(" , ") is None


def test_time_inputs_tc09():
    assert parse_time("7pm") is None
    assert parse_time("25:00") is None
    assert parse_time("7:30") == "07:30"
    assert parse_time("22:00") == "22:00"


def test_name_input():
    assert parse_name("   ") is None
    assert parse_name("x" * 101) is None
    assert parse_name("  Daily   devo ") == "Daily devo"


def interval(start, n):
    return {"schedule_type": "interval", "interval_days": n, "start_date": start, "weekdays": None}


def test_every_two_days_tc23():
    task = interval("2026-11-17", 2)
    assert task_is_due(task, date(2026, 11, 17))
    assert not task_is_due(task, date(2026, 11, 18))
    assert task_is_due(task, date(2026, 11, 19))
    assert not task_is_due(task, date(2026, 11, 16))


def test_weekdays_only_tc24():
    task = {"schedule_type": "weekly", "weekdays": "0,2,4"}
    due = [d for d in range(16, 23) if task_is_due(task, date(2026, 11, d))]
    assert due == [16, 18, 20]  # 16 Nov 2026 is a Monday
    assert schedule_description(task) == "on Monday, Wednesday, Friday"


def test_edit_restart_or_continue_tc44():
    day3, day4 = date(2026, 11, 19), date(2026, 11, 20)
    restart = interval("2026-11-18", 2)   # restarted on day 2
    continued = interval("2026-11-17", 2)  # kept the day-1 start
    assert (task_is_due(restart, day3), task_is_due(restart, day4)) == (False, True)
    assert (task_is_due(continued, day3), task_is_due(continued, day4)) == (True, False)


def test_reminder_passed():
    at = datetime(2026, 11, 17, 21, 0, 30)
    assert reminder_passed(at, "21:00")
    assert not reminder_passed(at, "21:01")


def view(task_id=1, name="Daily devo", people=((1, "Alice", True), (2, "Bob", False))):
    return TaskView(task_id, name, "21:00", [Person(*p) for p in people])


def test_reminder_has_one_button_per_person():
    text, keyboard = render_reminder(view(), "2026-11-17")
    buttons = [row[0] for row in keyboard.inline_keyboard]
    assert [b.text for b in buttons] == ["✅ Alice", "☐ Bob"]
    assert buttons[1].callback_data == "t:1:20261117:2"
    assert "Daily devo" in text and "21:00" in text


def test_overview_lists_tasks_and_names_in_buttons():
    text, keyboard = render_overview([view(1), view(2, "Gym")], "2026-11-17", "Asia/Singapore")
    assert "Daily devo" in text and "Gym" in text and "Alice, Bob" in text
    assert len(keyboard.inline_keyboard) == 4
    assert keyboard.inline_keyboard[2][0].text == "✅ Alice · Gym"
    assert day_from_code("20261117") == "2026-11-17"


def test_html_is_escaped():
    text, _ = render_reminder(view(name="<b>x</b> & y"), "2026-11-17")
    assert "&lt;b&gt;x&lt;/b&gt; &amp; y" in text


def test_long_overview_is_split():
    tasks = [view(i, "x" * 100, [(u, "Person " + str(u), False) for u in range(4)]) for i in range(60)]
    chunks = chunk_overview(tasks)
    assert len(chunks) > 1
    assert sum(len(c) for c in chunks) == 60
    for chunk in chunks:
        text, keyboard = render_overview(chunk, "2026-11-17", "Asia/Singapore", 1, len(chunks))
        assert len(text) <= 4096
        assert len(keyboard.inline_keyboard) <= 90


def test_split_text():
    text = "\n".join(["line %d" % i for i in range(2000)])
    pieces = split_text(text)
    assert all(len(p) <= MAX_MESSAGE_CHARS for p in pieces)
    assert "\n".join(pieces) == text
