# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
python3.12 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements-dev.txt   # runtime deps + pytest
export TELEGRAM_BOT_TOKEN=...                   # from @BotFather; never commit
python bot.py                                   # run locally (long polling)

python -m pytest                                # all tests (pytest.ini sets testpaths = tests)
python -m pytest tests/test_logic.py            # one file
python -m pytest tests/test_bot.py -k reminder  # one test by name
```

No linter or formatter is configured. Deploy is `fly deploy` (Fly.io, token via `fly secrets set`).

## Architecture

A Telegram group bot (python-telegram-bot v22, SQLite) split into three layers. Keep the split: `logic.py` and `storage.py` never import `bot.py`.

- **`logic.py`**: pure functions, no I/O. Input parsing (`parse_*`), schedule rules (`task_is_due`, `reminder_passed`), and rendering of overview/reminder messages and inline keyboards. Handles Telegram limits by chunking overviews (`MAX_MESSAGE_CHARS`, `MAX_BUTTONS`).
- **`storage.py`**: all SQL. `initialize_db()` creates the schema and upgrades older databases in place, so schema changes must keep existing rows. `task_view()` builds the `TaskView` that `logic.py` renders. The DB path comes from `ACCOUNTABILITY_DB_PATH`, read at import into `storage.DB_PATH`.
- **`bot.py`**: Telegram handlers, the `/addtask` and `/edittask` `ConversationHandler` flows (15-minute timeout, per-user drafts), and the scheduler.

### Things that span files

- **Scheduler**: `scheduler_tick` runs every 30 seconds via the job queue. `run_overviews` posts one overview per chat per day (also as catch-up after downtime), and `run_reminders` posts one reminder per task per day. Both are idempotent through DB markers (`overview_done`, `last_reminded_date`), so a restart does not double-post.
- **Message tracking and redraw**: every posted overview or reminder is recorded with `storage.record_message` (the `kind` is `reminder` or `overview N/M`). When someone ticks, `redraw_messages` edits every message showing that task for that day, which is how the overview and the reminder stay in sync. If you add a new message kind, record it and teach `redraw_messages` about it.
- **Due-day logging**: `due_log` and `completions` exist so habit metrics (80% and 7-day targets) can be computed even after a schedule is edited. Keep `log_due` calls when changing scheduling.
- **Chat migration**: a group upgrading to a supergroup changes its chat id; `on_migrate` calls `storage.migrate_chat` to move all rows.
- **Timezone**: one timezone for everything, `BOT_TIMEZONE` (default `Asia/Singapore`), resolved through `bot.now()`. Task "today" is computed in this zone, not UTC.

### Testing

`tests/conftest.py` gives every test a fresh temp SQLite DB (autouse, patches `storage.DB_PATH`), three members (`A`, `B`, `C`) in chat `CHAT`, a `FakeBot` that records `send_message` / `edit_message_text` calls, and a `make_task()` helper. Test names reference UAT case IDs; keep those when editing tests.

## Deployment notes

The Dockerfile copies files by name (`COPY bot.py logic.py storage.py ./`), so a new top-level module must be added there. Fly mounts a volume at `/data` for the database; back it up before deploying a new version. BotFather privacy mode must be **disabled**, or the bot cannot read plain-text answers in groups.
