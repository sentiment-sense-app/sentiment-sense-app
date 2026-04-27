from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm import LLMError, generate_bot_turn
from app.models import CheckinSession, Employee, Message, OnboardingToken, Report, now_utc
from app.telegram_client import TelegramAPIError, TelegramClient


logger = logging.getLogger(__name__)

INTRO_MESSAGE = (
    "Hi, I'm the company pulse assistant. I'll ask occasional questions about your work "
    "experience. Your responses may be summarized for HR so they can identify concerns "
    "and follow up appropriately. Please avoid sharing sensitive personal details unless necessary."
)

NO_ACTIVE_MESSAGE = (
    "There is no active check-in right now. HR will start one when needed. "
    "For testing, you can use /restart."
)

LLM_FALLBACK_MESSAGE = (
    "Thanks for your response. I had trouble processing that, but your message was saved. "
    "Please try again in a moment."
)


async def active_session_for_employee(db: AsyncSession, employee_id: int) -> CheckinSession | None:
    return (
        await db.execute(
            select(CheckinSession)
            .where(CheckinSession.employee_id == employee_id)
            .where(CheckinSession.status == "active")
            .order_by(CheckinSession.created_at.desc())
        )
    ).scalar_one_or_none()


async def latest_session_for_employee(db: AsyncSession, employee_id: int) -> CheckinSession | None:
    return (
        await db.execute(
            select(CheckinSession)
            .where(CheckinSession.employee_id == employee_id)
            .order_by(CheckinSession.created_at.desc())
        )
    ).scalars().first()


async def latest_report_for_employee(db: AsyncSession, employee_id: int) -> Report | None:
    return (
        await db.execute(
            select(Report)
            .where(Report.employee_id == employee_id)
            .order_by(Report.created_at.desc())
        )
    ).scalars().first()


async def checkin_label(db: AsyncSession, employee_id: int) -> str:
    session = await latest_session_for_employee(db, employee_id)
    if not session:
        return "Not started"
    if session.status == "active":
        return "Active"
    if session.status == "completed":
        return "Completed"
    return "Not started"


async def store_message(
    db: AsyncSession,
    employee: Employee,
    session: CheckinSession | None,
    direction: str,
    text: str,
    telegram_update_id: int | None = None,
    telegram_message_id: int | None = None,
    raw_payload: dict[str, Any] | None = None,
) -> Message:
    message = Message(
        session_id=session.id if session else None,
        employee_id=employee.id,
        telegram_update_id=telegram_update_id,
        telegram_message_id=telegram_message_id,
        direction=direction,
        text=text,
        raw_payload_json=json.dumps(raw_payload) if raw_payload else None,
    )
    db.add(message)
    await db.flush()
    return message


async def start_checkin_for_employee(
    db: AsyncSession,
    telegram: TelegramClient,
    employee: Employee,
    created_by_admin_id: int | None = None,
    cancel_existing: bool = False,
) -> tuple[bool, str]:
    if not employee.telegram_chat_id:
        return False, "Cannot send check-in yet. Ask the employee to open their onboarding link first."

    existing = await active_session_for_employee(db, employee.id)
    if existing and not cancel_existing:
        return False, "An active check-in already exists for this employee."
    if existing and cancel_existing:
        existing.status = "cancelled"
        existing.cancelled_at = now_utc()
        db.add(existing)

    session = CheckinSession(
        employee_id=employee.id,
        status="active",
        started_at=now_utc(),
        created_by_admin_id=created_by_admin_id,
    )
    db.add(session)
    await db.flush()
    first_message = f"Hi {employee.name}, quick check-in. How are you feeling about work this week?"
    await store_message(db, employee, session, "bot", first_message)

    try:
        await telegram.send_message(employee.telegram_chat_id, first_message)
    except TelegramAPIError as exc:
        session.status = "failed"
        db.add(session)
        await db.commit()
        return False, f"Telegram send failed: {exc}"

    await db.commit()
    return True, "Check-in sent."


async def handle_start_command(
    db: AsyncSession,
    telegram: TelegramClient,
    chat: dict[str, Any],
    user: dict[str, Any],
    payload: str | None,
) -> None:
    chat_id = str(chat.get("id"))
    if not payload:
        await telegram.send_message(chat_id, "Please use your company onboarding link to set up the pulse assistant.")
        return

    token = (
        await db.execute(select(OnboardingToken).where(OnboardingToken.token == payload.strip()))
    ).scalar_one_or_none()
    token_invalid = (
        not token
        or token.is_revoked
        or (token.expires_at is not None and token.expires_at <= now_utc())
        or (token.used_at is not None and str(token.employee.telegram_chat_id) != chat_id)
    )
    if token_invalid:
        await telegram.send_message(chat_id, "This onboarding link is invalid or expired. Please contact HR for a fresh link.")
        return

    employee = token.employee
    if employee.telegram_chat_id and str(employee.telegram_chat_id) == chat_id:
        await telegram.send_message(chat_id, "Setup is already complete. HR can now send pulse check-ins when needed.")
        return

    employee.telegram_chat_id = chat_id
    employee.telegram_user_id = str(user.get("id")) if user.get("id") is not None else None
    employee.telegram_username = user.get("username")
    employee.telegram_connected_at = now_utc()
    token.used_at = now_utc()
    db.add(employee)
    db.add(token)
    await db.commit()

    await telegram.send_message(chat_id, INTRO_MESSAGE)
    await start_checkin_for_employee(db, telegram, employee, cancel_existing=True)


async def handle_restart_command(db: AsyncSession, telegram: TelegramClient, employee: Employee) -> None:
    ok, message = await start_checkin_for_employee(db, telegram, employee, cancel_existing=True)
    if not ok and employee.telegram_chat_id:
        await telegram.send_message(employee.telegram_chat_id, message)


async def handle_cancel_command(db: AsyncSession, telegram: TelegramClient, employee: Employee) -> None:
    session = await active_session_for_employee(db, employee.id)
    if not session:
        await telegram.send_message(employee.telegram_chat_id, "There is no active check-in to cancel.")
        return
    session.status = "cancelled"
    session.cancelled_at = now_utc()
    db.add(session)
    await db.commit()
    await telegram.send_message(employee.telegram_chat_id, "The active check-in has been cancelled.")


async def handle_employee_message(
    db: AsyncSession,
    telegram: TelegramClient,
    employee: Employee,
    session: CheckinSession,
    text: str,
    update_id: int,
    message_id: int | None,
    raw_payload: dict[str, Any],
) -> None:
    await store_message(db, employee, session, "employee", text, update_id, message_id, raw_payload)
    await db.commit()

    messages = (
        await db.execute(
            select(Message).where(Message.session_id == session.id).order_by(Message.created_at.asc())
        )
    ).scalars().all()
    try:
        decision = await generate_bot_turn(employee, list(messages))
    except LLMError as exc:
        logger.warning("LLM failed for session %s: %s", session.id, exc)
        await store_message(db, employee, session, "bot", LLM_FALLBACK_MESSAGE)
        await db.commit()
        await telegram.send_message(employee.telegram_chat_id, LLM_FALLBACK_MESSAGE)
        return

    reply = decision["reply_to_employee"].strip()
    await store_message(db, employee, session, "bot", reply)
    if decision["conversation_done"]:
        session.status = "completed"
        session.completed_at = now_utc()
        db.add(session)
        db.add(
            Report(
                session_id=session.id,
                employee_id=employee.id,
                report_markdown=decision["report_markdown"].strip(),
                status="open",
            )
        )
    await db.commit()
    await telegram.send_message(employee.telegram_chat_id, reply)


async def process_telegram_update(db: AsyncSession, telegram: TelegramClient, update: dict[str, Any]) -> None:
    update_id = update.get("update_id")
    message = update.get("message") or {}
    text = (message.get("text") or "").strip()
    chat = message.get("chat") or {}
    user = message.get("from") or {}
    chat_id = str(chat.get("id")) if chat.get("id") is not None else ""
    if update_id is None or not text or not chat_id:
        return

    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        await handle_start_command(db, telegram, chat, user, parts[1] if len(parts) > 1 else None)
        return

    employee = (
        await db.execute(select(Employee).where(Employee.telegram_chat_id == chat_id))
    ).scalar_one_or_none()
    if not employee:
        await telegram.send_message(chat_id, "Please use your company onboarding link before sending messages here.")
        return

    command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
    if command in {"/restart", "/reset"}:
        await handle_restart_command(db, telegram, employee)
        return
    if command == "/cancel":
        await handle_cancel_command(db, telegram, employee)
        return
    if command == "/help":
        await telegram.send_message(
            chat_id,
            "I help with short workplace pulse check-ins. Use /restart to start a fresh test check-in or /cancel to stop the active one.",
        )
        return

    session = await active_session_for_employee(db, employee.id)
    if not session:
        await telegram.send_message(chat_id, NO_ACTIVE_MESSAGE)
        return

    await handle_employee_message(
        db,
        telegram,
        employee,
        session,
        text,
        int(update_id),
        message.get("message_id"),
        update,
    )
