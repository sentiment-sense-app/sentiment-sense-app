import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key-change-in-production")
OPENROUTER_KEY = os.environ.get("OPENROUTER_KEY", "")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "").lower() in ("1", "true", "yes")
DATABASE_URL = os.environ.get(
    "DATABASE_URL", f"sqlite+aiosqlite:///{BASE_DIR / 'survey.db'}"
)
AI_MODEL = "anthropic/claude-haiku-4.5"
MAX_QUESTIONS = 8
QUESTIONS_PER_ROUND = 4
SURVEY_EXPIRY_DAYS = 7
