import itertools
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import storage  # noqa: E402

CHAT = -100
A, B, C = 1, 2, 3


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "test.db")
    storage.initialize_db()
    for user_id, name in ((A, "Alice"), (B, "Bob"), (C, "Cara")):
        storage.join_member(CHAT, user_id, name, None)
    return tmp_path / "test.db"


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edited = []
        self._ids = itertools.count(1000)
        self.fail_with = None

    async def send_message(self, chat_id, text, **kwargs):
        if self.fail_with is not None:
            raise self.fail_with
        message = SimpleNamespace(message_id=next(self._ids), chat_id=chat_id, text=text, **kwargs)
        self.sent.append(message)
        return message

    async def edit_message_text(self, text, **kwargs):
        self.edited.append(SimpleNamespace(text=text, **kwargs))


@pytest.fixture
def bot():
    return FakeBot()


def make_task(name="Daily devo", schedule_type="interval", interval_days=1, weekdays=None,
              reminder_time="21:00", today="2026-11-17", assignees=(A, B), skip=False, chat=CHAT):
    return storage.create_task(chat, A, name, schedule_type, interval_days, weekdays, reminder_time,
                               today, assignees, skip)
