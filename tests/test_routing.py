"""Send fake Telegram updates through the real Application, so ConversationHandler routing is tested.

The other flow tests call handlers directly; the bugs below only show up in routing.
"""
import asyncio
import itertools
import json

import pytest
from telegram import Update
from telegram.request import BaseRequest

import bot as app
import storage
from conftest import CHAT, A, B, make_task
from test_bot import at

NAMES = {A: "Alice", B: "Bob"}


class FakeTelegram(BaseRequest):
    """Answers every Bot API call locally and records it."""

    def __init__(self):
        self.calls = []
        self._ids = itertools.count(500)

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    @property
    def read_timeout(self):
        return 5

    async def do_request(self, url, method, request_data=None, **_):
        name = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        result = True
        if name == "getMe":
            result = {"id": 99, "is_bot": True, "first_name": "Bot", "username": "bot"}
        elif name in ("sendMessage", "editMessageText", "editMessageReplyMarkup"):
            message_id = next(self._ids) if name == "sendMessage" else params["message_id"]
            result = {"message_id": message_id, "date": 0, "text": params.get("text", ""),
                      "chat": {"id": CHAT, "type": "supergroup"}}
        self.calls.append({"method": name, **params, "result": result})
        return 200, json.dumps({"ok": True, "result": result}).encode()


class Group:
    def __init__(self):
        # PTB objects must stay on one loop, and on Python 3.9 they need it current when built.
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.telegram = FakeTelegram()
        self.application = app.build_application("1:TEST", request=self.telegram)
        self._update_ids = itertools.count(1)
        self.loop.run_until_complete(self.application.initialize())

    def _process(self, raw):
        start = len(self.telegram.calls)
        self.loop.run_until_complete(self.application.process_update(Update.de_json(raw, self.application.bot)))
        return self.telegram.calls[start:]

    def send(self, user, text):
        entities = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}] \
            if text.startswith("/") else []
        return self._process({"update_id": next(self._update_ids), "message": {
            "message_id": next(self._update_ids), "date": 0, "text": text, "entities": entities,
            "chat": {"id": CHAT, "type": "supergroup"},
            "from": {"id": user, "is_bot": False, "first_name": NAMES[user]}}})

    def tap(self, user, data, message_id):
        return self._process({"update_id": next(self._update_ids), "callback_query": {
            "id": str(next(self._update_ids)), "chat_instance": "x", "data": data,
            "from": {"id": user, "is_bot": False, "first_name": NAMES[user]},
            "message": {"message_id": message_id, "date": 0, "text": "-",
                        "chat": {"id": CHAT, "type": "supergroup"},
                        "from": {"id": 99, "is_bot": True, "first_name": "Bot"}}}})


def texts(calls):
    return [call["text"] for call in calls if "text" in call]


def sent_id(calls):
    """Message ID of the last message the bot sent in these calls."""
    return [call for call in calls if call["method"] == "sendMessage"][-1]["result"]["message_id"]


def alerts(calls):
    return [call.get("text") for call in calls if call["method"] == "answerCallbackQuery"]


@pytest.fixture(autouse=True)
def fixed_time(monkeypatch):
    monkeypatch.setattr(app, "now", lambda: at("2026-11-17", "10:00"))


@pytest.fixture
def group():
    chat = Group()
    yield chat
    chat.loop.close()
    asyncio.set_event_loop(None)


def reach_people_step(group, user, name):
    group.send(user, "/addtask")
    schedule_message = sent_id(group.send(user, name))
    group.tap(user, "schedule:interval", schedule_message)
    group.send(user, "1")
    return sent_id(group.send(user, "22:00"))


def test_edittask_while_addtask_is_open_replaces_it_tc58(group):
    make_task()
    group.send(A, "/addtask")
    calls = group.send(A, "/edittask 1")
    assert "unfinished task creation was cancelled" in texts(calls)[0]
    group.tap(A, "edit:name", sent_id(calls))
    assert any("task updated" in text for text in texts(group.send(A, "Evening devo")))
    assert storage.get_task_by_number(CHAT, 1)["name"] == "Evening devo"
    assert texts(group.send(A, "/cancel")) == ["there is nothing to cancel."]


def test_bad_edittask_number_keeps_the_open_addtask(group):
    make_task()
    group.send(A, "/addtask")
    assert "couldn't find task 99" in texts(group.send(A, "/edittask 99"))[0]
    assert texts(group.send(A, "Gym")) == ["how often should it be done?"]


def test_own_flow_cannot_use_someone_elses_buttons_tc59(group):
    alice_list = reach_people_step(group, A, "Gym")
    bob_list = reach_people_step(group, B, "Read")

    for data in ("assign:done", f"assign:{B}"):
        calls = group.tap(B, data, alice_list)
        assert alerts(calls) == [app.STALE_BUTTON_TEXT]
        assert [call["method"] for call in calls] == ["answerCallbackQuery"]  # Alice's list untouched
    assert storage.active_tasks(CHAT) == []

    group.tap(B, "assign:done", bob_list)  # Bob can still finish his own
    group.tap(A, "assign:done", alice_list)
    tasks = {task["name"]: storage.task_member_ids(task["id"]) for task in storage.active_tasks(CHAT)}
    assert tasks == {"Read": {B}, "Gym": {A}}


def test_other_person_without_a_flow_is_turned_away_tc54(group):
    alice_list = reach_people_step(group, A, "Gym")
    assert alerts(group.tap(B, f"assign:{B}", alice_list)) == [app.STALE_BUTTON_TEXT]
    assert texts(group.send(B, "hijack")) == []
    group.tap(A, "assign:done", alice_list)
    assert storage.task_member_ids(storage.active_tasks(CHAT)[0]["id"]) == {A}
