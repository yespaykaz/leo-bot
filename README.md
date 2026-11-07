# Leo Telegram Dating Bot

A polling Telegram dating bot built with `python-telegram-bot`. It stores
profiles and likes in the local SQLite database `leo.db`.

## Run

1. Set the bot token in Replit Secrets as `TELEGRAM_TOKEN`.
2. Start the **Telegram Dating Bot** workflow, or run:

   ```bash
   uv run python main.py
   ```

The bot creates `leo.db` on first start. Do not commit the database or the
Telegram token.

## Included

- `/start` registration for adults 18+, including profile photo and preferences
- Random compatible profiles with Like and Dislike actions
- Mutual-like notifications to both users
- Profile display and editing
- Premium information: 1490 tg per week, contact `@leo_admin`

Run the local checks with:

```bash
uv run python -m unittest -v
```
