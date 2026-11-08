# Leo Telegram Dating Bot

A polling Telegram dating bot built with `python-telegram-bot`. It stores
profiles and likes in the local SQLite database `bot.db`.

## Run

1. Set the bot token in Replit Secrets as `TELEGRAM_TOKEN`.
2. Start the **Telegram Dating Bot** workflow, or run:

   ```bash
   uv run python main.py
   ```

The bot creates `bot.db` on first start. Existing profiles and likes in `leo.db`
are copied into `bot.db` without deleting the old file. Do not commit either
database or the Telegram token.

## Included

- `/start` registration for adults 18+, including profile photo and preferences
- Random compatible profiles with Like and Dislike actions
- Mutual-like notifications to both users
- Profile display and editing
- Persistent one-row menu: `1 🚀` browse, `2` profile, `3` pause, `4` edit, `5` Premium
- Premium information: 2000 tenge per week, contact `@leoqazaq`

Run the local checks with:

```bash
uv run python -m unittest -v
```
