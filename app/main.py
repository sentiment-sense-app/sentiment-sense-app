from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.auth import seed_admin
from app.config import settings
from app.database import async_session, init_db
from app.routes import auth_routes, dashboard_routes, employee_routes, survey_admin_routes, survey_routes
from app.telegram_client import TelegramAPIError, TelegramClient
from app.telegram_polling import polling_loop


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d %(levelname)-5s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    async with async_session() as db:
        await seed_admin(db)

    stop_event = asyncio.Event()
    polling_task: asyncio.Task | None = None
    if settings.telegram_polling_enabled and settings.telegram_bot_token:
        try:
            await TelegramClient().set_my_commands()
        except TelegramAPIError:
            logger.warning("Could not set Telegram bot commands during startup.")
        polling_task = asyncio.create_task(polling_loop(stop_event))

    yield

    if polling_task:
        stop_event.set()
        polling_task.cancel()
        try:
            await polling_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Sentiment Sense", lifespan=lifespan)

app.include_router(dashboard_routes.router)
app.include_router(auth_routes.router)
app.include_router(employee_routes.router)
app.include_router(survey_routes.router)
app.include_router(survey_admin_routes.router)
