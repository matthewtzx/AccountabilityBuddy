# AccountabilityBuddy

A Telegram group bot that reminds a small group to do shared recurring tasks
and lets each person tick off their own name.

## What it does

- `/join` lets a member be picked for tasks in that group.
- `/addtask` creates a task: name, schedule (every N days or chosen weekdays),
  reminder time and the people involved.
- At 12am on each day a task is due, the bot posts one overview of the day's
  tasks with a checklist: one button per person per task.
- At the reminder time it posts a separate reminder with the same buttons.
- Each person taps their own name to tick or untick. A tick posts
  "[name] has completed [task]", and the last tick also posts that everyone
  has completed it. The overview and the reminder both update.
- `/tasks` lists tasks and today's status. `/edittask NUMBER` and
  `/deletetask NUMBER` change or remove a task; anyone in the group can.
- `/cancel` stops creating or editing a task.

All times use one timezone, `Asia/Singapore` by default (`BOT_TIMEZONE`).
The bot only works in group chats.

## Files

| File | Holds |
|---|---|
| `bot.py` | Telegram handlers, the guided flows and the 30-second scheduler |
| `logic.py` | Parsing, schedule rules and message rendering (no I/O) |
| `storage.py` | SQLite tables, the upgrade of older databases, and all queries |
| `tests/` | pytest suite; acceptance tests, named by UAT case ID |

## 1. Create the bot

1. In Telegram, message **@BotFather**, send `/newbot` and follow the prompts.
2. Copy the token. Treat it like a password; never commit it.
3. Send `/setprivacy` to BotFather, pick the bot and choose **Disable**.
   Without this the bot cannot read plain-text answers to `/addtask` in a
   group. If the bot is already in a group, remove it and add it again
   afterwards: the change only applies when it joins.

## 2. Run it locally

Python 3.12 (the version the Docker image uses):

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
export TELEGRAM_BOT_TOKEN="your_token_from_botfather"
python bot.py
```

Data is saved in `accountability.db`; set `ACCOUNTABILITY_DB_PATH` to move it.
Older databases are upgraded in place on start, and existing rows are kept.

## 3. Tests

```bash
python -m pytest
```

To try the 12am overview without waiting, run the bot with a `BOT_TIMEZONE`
where midnight is a few minutes away.

## 4. Deploy (Fly.io)

`fly.toml` mounts a volume at `/data` for the database and sets the timezone.
Set the token as a secret, never in a file:

```bash
fly secrets set TELEGRAM_BOT_TOKEN=...
fly deploy
```

Before deploying a new version, copy `/data/accountability.db` off the volume
as a backup, e.g. with `fly ssh sftp get /data/accountability.db`.

## Reading habit metrics

Streaks are not shown in the bot. `completions` holds one row per person,
task and day ticked; `due_log` holds each task's due days, so the 80%
and 7-day targets can be computed even after a schedule is edited.
