# Sentiment Sense

Confidential employee pulse assistant. HR imports employees, the bot DMs them on Telegram, has a short AI-led check-in, and produces an HR-facing report per completed conversation.

## Stack

FastAPI + Jinja2 + SQLite (SQLAlchemy async) + Telegram Bot API (httpx) + OpenRouter (via OpenAI SDK) + Bootstrap.

## Setup

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync
cp .env .env.local  # optional; or edit .env directly
```

Fill in `.env`:

```env
SECRET_KEY=...                 # python -c 'import secrets; print(secrets.token_hex(32))'
OPENROUTER_KEY=sk-or-...
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=admin123
TELEGRAM_BOT_TOKEN=            # from @BotFather
TELEGRAM_BOT_USERNAME=         # bot's @username, no @
```

Run:

```bash
uv run uvicorn app.main:app --reload
```

Open http://localhost:8000. Admin user is seeded from `ADMIN_EMAIL` / `ADMIN_PASSWORD` on first startup.

## Flow

1. Admin logs in, imports employees from CSV (required: `name`, `email`, `department`, `manager`, `project`, `role`, `phone`).
2. Each employee gets a personalized Telegram deep link `https://t.me/<bot>?start=<token>`.
3. Admin sends the link to the employee out-of-band. Employee opens it and presses Start; the bot stores their `chat_id`.
4. Admin clicks **Send check-in**. Bot DMs the employee.
5. Employee replies. The LLM decides the next question or wraps up the conversation. When done, an HR-facing Markdown report is saved.
6. Admin reviews transcripts, marks reports `open` / `reviewed` / `resolved` / `dismissed`, and exports CSV.

## Bot Commands

- `/start <token>` — onboarding (issued via deep link)
- `/restart` or `/reset` — start a fresh check-in (testing)
- `/cancel` — cancel the active check-in
- `/help` — usage hint

## Layout

```
app/
  main.py              FastAPI app + lifespan (init DB, seed admin, start polling)
  config.py            pydantic-settings env loader
  database.py          async SQLAlchemy engine + session
  models.py            ORM models
  auth.py              admin auth, sessions, CSRF
  web.py               Jinja2 templates + filters
  csv_import.py        employee CSV ingest
  llm.py               OpenRouter call + JSON contract
  bot_logic.py         Telegram update handling, check-in lifecycle
  telegram_client.py   thin httpx wrapper around Bot API
  telegram_polling.py  long-poll loop run from FastAPI lifespan
  reports.py           CSV export + status helpers
  routes/              auth / dashboard / employee / checkin / report
  templates/           Jinja2 pages
```

## Deployment

Docker Compose with Caddy (auto-HTTPS) in front of the app. SQLite persisted in a named Docker volume — no DB container. Single uvicorn worker so only one Telegram polling loop runs.

### Update existing deployment

```bash
ssh root@168.144.25.202 'cd /opt/sentiment-sense-app && git pull && docker compose up -d --build'
```

After this swap, the prod `.env` needs the new vars added (`ADMIN_EMAIL`, `ADMIN_PASSWORD`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME`). Old `survey.db` in the volume is left alone; the new app uses `app.db`.

### Backup

```bash
docker compose cp app:/app/data/app.db ./app-$(date +%F).db
```

## Reset During Demo Development

No migrations. If the schema changes, stop the app and delete the SQLite file:

```bash
rm app.db
```

For Docker:

```bash
docker compose down && docker volume rm sentiment-sense_app_data && docker compose up -d --build
```
