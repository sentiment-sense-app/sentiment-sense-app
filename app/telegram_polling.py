import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.bot_logic import process_telegram_update
from app.database import async_session
from app.models import AppSetting
from app.telegram_client import TelegramAPIError, TelegramClient


logger = logging.getLogger(__name__)
POLLING_OFFSET_KEY = "telegram_last_update_id"


async def get_last_update_id(db: AsyncSession) -> int | None:
    setting = await db.get(AppSetting, POLLING_OFFSET_KEY)
    return int(setting.value) if setting and setting.value else None


async def set_last_update_id(db: AsyncSession, update_id: int) -> None:
    setting = await db.get(AppSetting, POLLING_OFFSET_KEY)
    if setting:
        setting.value = str(update_id)
    else:
        setting = AppSetting(key=POLLING_OFFSET_KEY, value=str(update_id))
        db.add(setting)
    await db.commit()


async def polling_loop(stop_event: asyncio.Event) -> None:
    telegram = TelegramClient()
    if not telegram.configured:
        logger.info("Telegram polling disabled because TELEGRAM_BOT_TOKEN is not configured.")
        return

    # A leftover webhook registration silently swallows updates from getUpdates.
    try:
        await telegram.delete_webhook()
    except TelegramAPIError:
        logger.warning("Could not clear Telegram webhook before polling.")

    while not stop_event.is_set():
        try:
            async with async_session() as db:
                last_update_id = await get_last_update_id(db)
                offset = last_update_id + 1 if last_update_id is not None else None
                updates = await telegram.get_updates(offset=offset, timeout=25)
                for update in updates:
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        await set_last_update_id(db, update_id)
                    try:
                        await process_telegram_update(db, telegram, update)
                    except Exception:
                        logger.exception("Failed to process Telegram update %s", update_id)
        except TelegramAPIError:
            logger.exception("Telegram polling request failed")
            await asyncio.sleep(5)
        except Exception:
            logger.exception("Unexpected error in Telegram polling loop")
            await asyncio.sleep(5)
        await asyncio.sleep(0.25)
