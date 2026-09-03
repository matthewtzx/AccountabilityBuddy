# Telegram Accountability Bot

This Python bot creates recurring accountability tasks, sends reminders at a
chosen time, and lets users mark tasks complete with a Telegram button. Tasks
are stored in a local SQLite database and survive bot restarts.

## Features

- Create a task with `/addtask`.
- Repeat it every N days or on selected days of the week.
- Select one or more registered people who must complete it.
- Receive a reminder at a chosen time.
- Let each assignee mark only themselves complete from the reminder button.
- See everyone's individual completion status in the group.
- View the task list with `/deletetask`, or remove one with `/deletetask NUMBER`.
- Keep task numbers consecutive after a task is removed.

## 1. Create a Telegram bot

1. Open Telegram and start a chat with **@BotFather**.
2. Send `/newbot` and follow the prompts.
3. Copy the bot token BotFather gives you. Treat it like a password.

## 2. Set up the project

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1`.

## 3. Set the token and run the bot

On macOS or Linux:

```bash
export TELEGRAM_BOT_TOKEN="your_token_from_botfather"
python bot.py
```

On Windows PowerShell:

```powershell
$env:TELEGRAM_BOT_TOKEN="your_token_from_botfather"
python bot.py
```

The default timezone is `Asia/Singapore`. To use another IANA timezone, set it
before starting the bot, for example `export BOT_TIMEZONE="America/New_York"`.

Open your bot in Telegram and send `/start`. In a group, every person who should
be selectable must send `/join` once. Telegram's Bot API does not provide bots
with a complete list of ordinary group members, so the bot can only offer people
who have interacted with it in that group. After everyone has joined, use
`/addtask` and select the assignees during setup.

When a reminder arrives, an assigned person can press **Mark myself complete**.
The group message updates that person's status only and keeps the other members
pending until they complete it themselves.

Keep the terminal open while the bot is running. Press `Ctrl+C` to stop it.

Tasks are saved in `accountability.db`. Override the location with the
`ACCOUNTABILITY_DB_PATH` environment variable if needed.
