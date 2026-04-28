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
SECRET_KEY=...                       # python -c 'import secrets; print(secrets.token_hex(32))'
OPENROUTER_KEY=sk-or-...
OPENROUTER_MODEL=deepseek/deepseek-v4-flash   # optional, defaults to deepseek/deepseek-v4-pro
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=admin123
TELEGRAM_BOT_TOKEN=                  # from @BotFather
TELEGRAM_BOT_USERNAME=               # bot's @username, no @
TELEGRAM_POLLING_ENABLED=true
```

Run:

```bash
uv run uvicorn app.main:app --reload
```

Open http://localhost:8000. Admin user is seeded from `ADMIN_EMAIL` / `ADMIN_PASSWORD` on first startup.

## Flow

1. Admin logs in, imports employees from CSV (required: `name`, `email`, `department`, `manager`, `project`, `role`, `phone`). A ready-to-use `demo_employees.csv` ships in the repo for quick testing.
2. Each employee gets a personalized Telegram deep link `https://t.me/<bot>?start=<token>`.
3. Admin clicks **Send survey** on the employees page → form for total questions, % from custom list (manual textarea or CSV upload). If the employee is already connected on Telegram, the first question is sent immediately. If not, the survey is queued.
4. Admin sends the deep link to the employee out-of-band. Employee opens it and presses Start; the bot stores their `chat_id`. If a queued survey is waiting, it activates and the first question goes out right away — otherwise the bot just confirms they're connected and waits for HR.
5. Employee replies on Telegram. The LLM asks follow-ups up to the question budget; the bot allows up to 20% extra turns for tactful follow-ups before force-finalizing. When the survey ends, an HR-facing Markdown report is saved and a "survey complete" message is sent to the employee.
6. Admin reviews surveys on the dashboard (grouped by employee), marks them `open` / `reviewed` / `resolved` / `dismissed`, and exports CSV.

## Bot Commands

- `/start <token>` — onboarding (issued via deep link)
- `/restart` or `/reset` — start a fresh survey (testing)
- `/cancel` — cancel the active survey
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
  bot_logic.py         Telegram update handling, survey lifecycle, custom-question selection
  telegram_client.py   thin httpx wrapper around Bot API
  telegram_polling.py  long-poll loop run from FastAPI lifespan
  reports.py           CSV export + status helpers
  routes/              auth / dashboard / employee / survey / survey_admin
  templates/           Jinja2 pages (dashboard groups surveys per employee)
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
