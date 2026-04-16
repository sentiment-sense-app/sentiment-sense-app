# Sentiment Sense

AI-powered dynamic employee survey tool. Generates adaptive questions per employee (based on role, project, tenure, and prior answers) to capture sentiment and surface attrition risk.

## Stack

FastAPI + Jinja2 + SQLite (SQLAlchemy async) + OpenRouter (via OpenAI SDK) + Bootstrap.

## Setup

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync
```

Set environment variables:

```bash
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export OPENROUTER_KEY="sk-or-..."
```

Create an admin user:

```bash
uv run python cli.py
```

Run:

```bash
uv run uvicorn app.main:app --reload
```

Open http://localhost:8000.

## Flow

1. Admin logs in, uploads employee CSV (required: `name`, `email`; optional: `role`, `project`, `experience_years`).
2. A survey session + single-use UUID link is auto-created per employee.
3. Employee opens link → receives AI-generated questions tailored to their profile.
4. On submit, the system feeds prior Q&A back to the LLM and generates the next round. Max 8 questions.
5. Admin views responses on the results page.

## Layout

```
app/
  main.py       FastAPI app, middleware, routing
  config.py     env vars, constants
  core.py       auth, CSRF, limiter, templating helpers
  database.py   engine, session, init_db
  models.py     SQLAlchemy models
  admin.py      admin routes (login, upload, dashboard, results)
  survey.py     employee survey routes
  services/
    ai.py       question generation
  templates/    Jinja2 templates
cli.py          admin creation CLI
```

## Security

- Session cookies (signed via `itsdangerous`) for admin auth
- CSRF tokens on all admin forms
- bcrypt password hashing
- Rate limiting on login (`slowapi`)
- Single-use UUID4 tokens for employee links, 7-day TTL
- Security headers (X-Frame-Options, X-Content-Type-Options, Referrer-Policy)
- `/docs` and `/redoc` disabled (internal tool)

## Deployment

Docker Compose with Caddy (auto-HTTPS) in front of the app. SQLite persisted in a named Docker volume - no DB container.

### One-time VM setup

Any small Linux VM with SSH. Point the domain's A record at the VM's IP.

```bash
apt update && apt install -y docker.io docker-compose-plugin git ufw
ufw allow 22,80,443/tcp && ufw --force enable
```

### Deploy

On the VM:

```bash
git clone <repo-url> sentiment-sense && cd sentiment-sense

cat > .env <<EOF
SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
OPENROUTER_KEY=sk-or-...
EOF

docker compose up -d --build
docker compose exec app uv run python cli.py   # create admin
```

Update the domain in `Caddyfile` before first run if not using `sentiment-sense.air-app.xyz`.

### Updates

```bash
git pull && docker compose up -d --build
```

### Backup

```bash
docker compose cp app:/app/data/survey.db ./survey-$(date +%F).db
```
