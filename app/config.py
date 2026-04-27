from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    secret_key: str = "change-me"
    public_base_url: str = "http://localhost:8000"

    database_url: str = "sqlite+aiosqlite:///./app.db"

    admin_email: str = "admin@example.com"
    admin_password: str = "admin123"

    telegram_bot_token: str = ""
    telegram_bot_username: str = ""
    telegram_polling_enabled: bool = True

    openrouter_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "deepseek/deepseek-v4-pro"

    session_cookie_name: str = "hr_pulse_session"
    session_ttl_hours: int = 12

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def cookie_secure(self) -> bool:
        return self.app_env.lower() == "production"

    @property
    def telegram_bot_url(self) -> str:
        username = self.telegram_bot_username.strip().lstrip("@")
        return f"https://t.me/{username}" if username else ""


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
