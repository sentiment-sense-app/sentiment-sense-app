from __future__ import annotations

import json
import logging
import math
import random
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.focus_areas import normalize_focus_slugs
from app.llm import LLMError, generate_bot_turn
from app.models import Employee, Message, OnboardingToken, Report, Survey, now_utc
from app.telegram_client import TelegramAPIError, TelegramClient


logger = logging.getLogger(__name__)

INTRO_MESSAGE = (
    "👋 Hi! I'm Sentiment Sense, Accion's confidential pulse assistant. I'll ask a few "
    "short questions about how things are going at work. Your responses go to HR in a "
    "summarized form so they can support you and the team. Please skip anything you'd "
    "rather not share, and avoid sensitive personal details."
)

NO_ACTIVE_MESSAGE = (
    "There is no active survey right now. HR will start one when needed. "
    "For testing, you can use /restart."
)

LLM_FALLBACK_MESSAGE = (
    "Thanks for your response. I had trouble processing that, but your message was saved. "
    "Please try again in a moment."
)

SURVEY_COMPLETE_MESSAGE = (
    "✅ Your survey is complete. Thank you for sharing your feedback — your responses "
    "are confidential and will help HR support the team."
)

DEFAULT_TOTAL_QUESTIONS = 3
TURN_CAP_BUFFER_PCT = 20
TURN_CAP_MIN_BUFFER = 2

SURVEY_STATUSES = ["pending", "active", "completed", "cancelled", "failed"]


def survey_status_label(status: str) -> str:
    return (status or "").replace("_", " ").title() or "—"


def compute_turn_cap(total_questions: int) -> int:
    extra = max(TURN_CAP_MIN_BUFFER, math.ceil(total_questions * TURN_CAP_BUFFER_PCT / 100))
    return total_questions + extra


def pick_custom_questions(custom_questions: list[str], total_questions: int, custom_percent: int) -> list[str]:
    if not custom_questions or total_questions <= 0 or custom_percent <= 0:
        return []
    target = min(len(custom_questions), max(1, round(total_questions * custom_percent / 100)))
    pool = list(custom_questions)
    random.shuffle(pool)
    return pool[:target]


async def active_survey_for_employee(db: AsyncSession, employee_id: int) -> Survey | None:
    return (
        await db.execute(
            select(Survey)
            .where(Survey.employee_id == employee_id)
            .where(Survey.status == "active")
            .order_by(Survey.created_at.desc())
        )
    ).scalars().first()


async def pending_survey_for_employee(db: AsyncSession, employee_id: int) -> Survey | None:
    return (
        await db.execute(
            select(Survey)
            .where(Survey.employee_id == employee_id)
            .where(Survey.status == "pending")
            .order_by(Survey.created_at.desc())
        )
    ).scalars().first()


async def latest_survey_for_employee(db: AsyncSession, employee_id: int) -> Survey | None:
    return (
        await db.execute(
            select(Survey)
            .where(Survey.employee_id == employee_id)
            .order_by(Survey.created_at.desc())
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


async def survey_label(db: AsyncSession, employee_id: int) -> str:
    survey = await latest_survey_for_employee(db, employee_id)
    if not survey:
        return "Not started"
    if survey.status == "active":
        return "Active"
    if survey.status == "pending":
        return "Pending"
    if survey.status == "completed":
        return "Completed"
    return "Not started"


async def store_message(
    db: AsyncSession,
    employee: Employee,
    survey: Survey | None,
    direction: str,
    text: str,
    telegram_update_id: int | None = None,
    telegram_message_id: int | None = None,
    raw_payload: dict[str, Any] | None = None,
) -> Message:
    message = Message(
        survey_id=survey.id if survey else None,
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


def _opening_question(employee: Employee, custom_questions: list[str]) -> str:
    if custom_questions:
        return f"Hi {employee.name}, quick survey. {custom_questions[0]}"
    return f"Hi {employee.name}, quick survey. How are you feeling about work this week?"


async def start_survey_for_employee(
    db: AsyncSession,
    telegram: TelegramClient,
    employee: Employee,
    created_by_admin_id: int | None = None,
    cancel_existing: bool = False,
    total_questions: int = DEFAULT_TOTAL_QUESTIONS,
    custom_questions: list[str] | None = None,
    custom_percent: int = 0,
    focus_areas: list[str] | None = None,
) -> tuple[bool, str]:
    existing = await active_survey_for_employee(db, employee.id)
    pending = await pending_survey_for_employee(db, employee.id)
    if (existing or pending) and not cancel_existing:
        return False, "A survey is already queued or active for this employee."
    for prior in (existing, pending):
        if prior and cancel_existing:
            prior.status = "cancelled"
            prior.cancelled_at = now_utc()
            db.add(prior)

    customs = pick_custom_questions(custom_questions or [], total_questions, custom_percent)
    focus_slugs = normalize_focus_slugs(focus_areas)
    has_chat = bool(employee.telegram_chat_id)
    survey = Survey(
        employee_id=employee.id,
        status="active" if has_chat else "pending",
        started_at=now_utc() if has_chat else None,
        created_by_admin_id=created_by_admin_id,
        total_questions=total_questions,
        custom_percent=custom_percent,
        custom_questions_json=json.dumps(customs),
        focus_areas_json=json.dumps(focus_slugs),
        turn_cap=compute_turn_cap(total_questions),
    )
    db.add(survey)
    await db.flush()

    if not has_chat:
        await db.commit()
        return True, "Survey queued. It will start when the employee opens their onboarding link."

    first_message = _opening_question(employee, customs)
    await store_message(db, employee, survey, "bot", first_message)

    try:
        await telegram.send_message(employee.telegram_chat_id, first_message)
    except TelegramAPIError as exc:
        survey.status = "failed"
        db.add(survey)
        await db.commit()
        return False, f"Telegram send failed: {exc}"

    await db.commit()
    return True, "Survey sent."


async def _activate_pending_survey(
    db: AsyncSession,
    telegram: TelegramClient,
    employee: Employee,
    survey: Survey,
) -> None:
    customs = json.loads(survey.custom_questions_json or "[]")
    survey.status = "active"
    survey.started_at = now_utc()
    db.add(survey)
    first_message = _opening_question(employee, customs)
    await store_message(db, employee, survey, "bot", first_message)
    try:
        await telegram.send_message(employee.telegram_chat_id, first_message)
    except TelegramAPIError as exc:
        logger.warning("Failed to send opening message for survey %s: %s", survey.id, exc)
        survey.status = "failed"
        db.add(survey)
        await db.commit()
        return
    await db.commit()


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
        await telegram.send_message(chat_id, "Setup is already complete. HR can now send pulse surveys when needed.")
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

    pending = await pending_survey_for_employee(db, employee.id)
    if pending:
        await _activate_pending_survey(db, telegram, employee, pending)
    else:
        await telegram.send_message(
            chat_id,
            "You're connected. HR will start a survey when one is ready — no action needed from you right now.",
        )


async def handle_restart_command(db: AsyncSession, telegram: TelegramClient, employee: Employee) -> None:
    ok, message = await start_survey_for_employee(db, telegram, employee, cancel_existing=True)
    if not ok and employee.telegram_chat_id:
        await telegram.send_message(employee.telegram_chat_id, message)


async def handle_cancel_command(db: AsyncSession, telegram: TelegramClient, employee: Employee) -> None:
    survey = await active_survey_for_employee(db, employee.id)
    if not survey:
        await telegram.send_message(employee.telegram_chat_id, "There is no active survey to cancel.")
        return
    survey.status = "cancelled"
    survey.cancelled_at = now_utc()
    db.add(survey)
    await db.commit()
    await telegram.send_message(employee.telegram_chat_id, "The active survey has been cancelled.")


async def _finalize_survey(
    db: AsyncSession,
    employee: Employee,
    survey: Survey,
    report_markdown: str,
    sentiment_score: int | None = None,
) -> None:
    survey.status = "completed"
    survey.completed_at = now_utc()
    db.add(survey)
    db.add(
        Report(
            survey_id=survey.id,
            employee_id=employee.id,
            report_markdown=report_markdown.strip(),
            sentiment_score=sentiment_score,
            status="open",
        )
    )


async def handle_employee_message(
    db: AsyncSession,
    telegram: TelegramClient,
    employee: Employee,
    survey: Survey,
    text: str,
    update_id: int,
    message_id: int | None,
    raw_payload: dict[str, Any],
) -> None:
    logger.info("Employee msg from %s (survey=%d): %r", employee.name, survey.id, text[:80])
    await store_message(db, employee, survey, "employee", text, update_id, message_id, raw_payload)
    await db.commit()

    messages = (
        await db.execute(
            select(Message).where(Message.survey_id == survey.id).order_by(Message.created_at.asc())
        )
    ).scalars().all()
    bot_turns = sum(1 for m in messages if m.direction == "bot")
    force_finalize = bot_turns >= survey.turn_cap
    customs = json.loads(survey.custom_questions_json or "[]")
    focus_slugs = json.loads(survey.focus_areas_json or "[]")

    try:
        decision, usage = await generate_bot_turn(
            employee,
            list(messages),
            total_questions=survey.total_questions,
            turn_cap=survey.turn_cap,
            custom_questions=customs,
            focus_areas=focus_slugs,
            force_finalize=force_finalize,
        )
        survey.prompt_tokens += usage["prompt_tokens"]
        survey.completion_tokens += usage["completion_tokens"]
        survey.cost_usd += usage["cost_usd"]
        db.add(survey)
    except LLMError as exc:
        logger.warning("LLM failed for survey %s: %s", survey.id, exc)
        if force_finalize:
            placeholder = (
                "## Summary\nReport could not be generated automatically; please review the transcript.\n\n"
                "## Sentiment\nUnknown — LLM unavailable at finalize.\n\n"
                "## Key Themes\n- See transcript\n\n"
                "## Notable Quotes\n- See transcript\n\n"
                "## Suggested Follow-up\n1. Review the conversation transcript manually."
            )
            await _finalize_survey(db, employee, survey, placeholder)
            await db.commit()
            await telegram.send_message(employee.telegram_chat_id, SURVEY_COMPLETE_MESSAGE)
            return
        await store_message(db, employee, survey, "bot", LLM_FALLBACK_MESSAGE)
        await db.commit()
        await telegram.send_message(employee.telegram_chat_id, LLM_FALLBACK_MESSAGE)
        return

    reply = decision["reply_to_employee"].strip()
    await store_message(db, employee, survey, "bot", reply)
    completed = decision["conversation_done"] or force_finalize
    if completed:
        report_md = decision.get("report_markdown") or ""
        score = decision.get("sentiment_score")
        if not isinstance(score, int):
            score = None
        if not report_md.strip():
            report_md = (
                "## Summary\nReport text was missing from the model response.\n\n"
                "## Sentiment\nUnknown.\n\n"
                "## Key Themes\n- See transcript\n\n"
                "## Notable Quotes\n- See transcript\n\n"
                "## Suggested Follow-up\n1. Review the conversation transcript manually."
            )
        await _finalize_survey(db, employee, survey, report_md, sentiment_score=score)
        logger.info(
            "Survey complete for %s (survey=%d), report saved (score=%s)",
            employee.name,
            survey.id,
            score,
        )
    await db.commit()
    await telegram.send_message(employee.telegram_chat_id, reply)
    if completed:
        await telegram.send_message(employee.telegram_chat_id, SURVEY_COMPLETE_MESSAGE)
    logger.info("Bot reply sent to %s: %r", employee.name, reply[:80])


async def process_telegram_update(db: AsyncSession, telegram: TelegramClient, update: dict[str, Any]) -> None:
    update_id = update.get("update_id")
    message = update.get("message") or {}
    text = (message.get("text") or "").strip()
    chat = message.get("chat") or {}
    user = message.get("from") or {}
    chat_id = str(chat.get("id")) if chat.get("id") is not None else ""
    if update_id is None or not text or not chat_id:
        return

    if isinstance(update_id, int):
        seen = (
            await db.execute(
                select(Message.id).where(Message.telegram_update_id == update_id).limit(1)
            )
        ).scalar_one_or_none()
        if seen is not None:
            logger.info("Skipping already-processed Telegram update %s", update_id)
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
            "I help with short workplace pulse surveys. Use /restart to start a fresh test survey or /cancel to stop the active one.",
        )
        return

    survey = await active_survey_for_employee(db, employee.id)
    if not survey:
        await telegram.send_message(chat_id, NO_ACTIVE_MESSAGE)
        return

    await handle_employee_message(
        db,
        telegram,
        employee,
        survey,
        text,
        int(update_id),
        message.get("message_id"),
        update,
    )
