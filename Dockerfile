FROM python:3.12-slim

# Install uv from the official image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install dependencies first for better layer caching
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Copy application code
COPY app ./app
COPY cli.py ./

# Install the project itself
RUN uv sync --frozen --no-dev

# Persist SQLite DB outside the image; run as non-root
RUN useradd --system --create-home app \
    && mkdir -p /app/data \
    && chown -R app:app /app
USER app

ENV DATABASE_URL="sqlite+aiosqlite:////app/data/survey.db"

EXPOSE 8000
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
