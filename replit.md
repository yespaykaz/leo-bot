# Leo Telegram Dating Bot

A Telegram dating bot with profile registration, random profile browsing, likes, mutual-match notifications, and SQLite storage.

## Run & Operate

- `uv run python main.py` — start the bot with Telegram long polling
- `uv run python -m unittest -v` — run the local checks
- Required secret: `TELEGRAM_TOKEN`
- SQLite data is stored in `leo.db` (created on first run)
- `pnpm --filter @workspace/api-server run dev` — run the API server (port 5000)
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- Required env: `DATABASE_URL` — Postgres connection string

## Stack

- pnpm workspaces, Node.js 24, TypeScript 5.9
- API: Express 5
- DB: PostgreSQL + Drizzle ORM
- Validation: Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Build: esbuild (CJS bundle)

## Where things live

- `main.py` — Telegram bot, registration flow, profile browsing, and SQLite data layer
- `test_main.py` — database and matching behavior checks
- `pyproject.toml` / `uv.lock` — Python dependency manifest and lockfile

## Architecture decisions

- Telegram Bot API long polling is used; no webhook server is required.
- Profile photos are stored as Telegram `file_id` values, not downloaded image files.
- Profile details and likes are persisted in the local SQLite database.

## Product

- Users register an adult dating profile, browse mutually compatible profiles, and like or pass.
- A mutual like sends a notification to both users.
- Premium information points users to `@leo_admin`; payment is not processed in the bot.

## User preferences

- Keep the bot entry point in `main.py` and read its token from `TELEGRAM_TOKEN`.

## Gotchas

- Start only one polling instance per Telegram bot token.

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
