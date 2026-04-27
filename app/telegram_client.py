import logging
from typing import Any

import httpx

from app.config import settings


logger = logging.getLogger(__name__)


class TelegramAPIError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, token: str | None = None) -> None:
        self.token = token if token is not None else settings.telegram_bot_token
        self.base_url = f"https://api.telegram.org/bot{self.token}" if self.token else ""

    @property
    def configured(self) -> bool:
        return bool(self.token)

    async def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.configured:
            raise TelegramAPIError("TELEGRAM_BOT_TOKEN is not configured")
        try:
            async with httpx.AsyncClient(timeout=35) as client:
                response = await client.post(f"{self.base_url}/{method}", json=payload)
                response.raise_for_status()
                data = response.json()
        except httpx.TimeoutException as exc:
            logger.warning("Telegram request timed out: %s", method)
            raise TelegramAPIError("Telegram request timed out") from exc
        except httpx.HTTPStatusError as exc:
            logger.warning("Telegram HTTP error for %s: %s", method, exc.response.text)
            raise TelegramAPIError("Telegram API HTTP error") from exc
        except httpx.HTTPError as exc:
            logger.warning("Telegram network error for %s: %s", method, exc)
            raise TelegramAPIError("Telegram network error") from exc

        if not data.get("ok"):
            description = data.get("description", "Unknown Telegram API error")
            logger.warning("Telegram API error for %s: %s", method, description)
            raise TelegramAPIError(description)
        return data["result"]

    async def get_updates(self, offset: int | None = None, timeout: int = 25) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        return await self._post("getUpdates", payload)

    async def send_message(self, chat_id: str | int, text: str) -> dict[str, Any]:
        return await self._post("sendMessage", {"chat_id": chat_id, "text": text})

    async def get_me(self) -> dict[str, Any]:
        return await self._post("getMe", {})

    async def delete_webhook(self, drop_pending_updates: bool = False) -> dict[str, Any]:
        return await self._post("deleteWebhook", {"drop_pending_updates": drop_pending_updates})

    async def set_my_commands(self) -> dict[str, Any]:
        commands = [
            {"command": "restart", "description": "Start a fresh pulse survey"},
            {"command": "reset", "description": "Reset and start again"},
            {"command": "cancel", "description": "Cancel the active survey"},
            {"command": "help", "description": "Show help"},
        ]
        return await self._post("setMyCommands", {"commands": commands})
