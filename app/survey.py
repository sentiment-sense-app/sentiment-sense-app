import json
import math
import random
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import MAX_QUESTIONS, QUESTIONS_PER_ROUND
from app.core import render, verify_csrf_token
from app.database import get_db
from app.models import Question, Response, SurveySession, SurveyStatus
from app.services.ai import generate_questions

router = APIRouter()


async def _get_session(db: AsyncSession, token: str) -> SurveySession | None:
    result = await db.execute(
        select(SurveySession)
        .where(SurveySession.token == token)
        .options(
            selectinload(SurveySession.employee),
            selectinload(SurveySession.questions).selectinload(Question.response),
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        return None
    if session.status != SurveyStatus.EXPIRED.value and session.expires_at < datetime.utcnow():
        session.status = SurveyStatus.EXPIRED.value
        await db.commit()
    return session


def _pick_customs_for_round(session: SurveySession, batch_size: int, remaining: int) -> list[dict]:
    """Return the custom questions to include in the next round (first N from unused pool)."""
    if not session.custom_questions:
        return []
    all_customs = json.loads(session.custom_questions)
    used_count = sum(1 for q in session.questions if q.is_custom)
    unused = all_customs[used_count:]
    if not unused:
        return []
    rounds_remaining = max(1, math.ceil(remaining / QUESTIONS_PER_ROUND))
    customs_this_round = min(len(unused), math.ceil(len(unused) / rounds_remaining), batch_size)
    return unused[:customs_this_round]


async def _generate_next_batch(
    session: SurveySession, db: AsyncSession, employee_dict: dict, prior_qa: list
) -> None:
    """Generate and insert the next batch of questions (custom + LLM) for the current round."""
    total = len(session.questions)
    remaining = MAX_QUESTIONS - total
    if remaining <= 0:
        return

    batch_size = min(QUESTIONS_PER_ROUND, remaining)
    customs = _pick_customs_for_round(session, batch_size, remaining)
    llm_count = batch_size - len(customs)

    llm_questions: list[dict] = []
    if llm_count > 0:
        llm_questions = await generate_questions(
            employee_dict,
            prior_qa,
            session.focus_area,
            llm_count,
            customs_in_round=customs,
        )

    # Mark origin, combine, and shuffle within the round so customs aren't clumped.
    batch = [dict(q, _is_custom=True) for q in customs] + [dict(q, _is_custom=False) for q in llm_questions]
    random.shuffle(batch)

    for q in batch:
        db.add(
            Question(
                session_id=session.id,
                round_number=session.current_round,
                question_text=q["text"],
                question_type=q.get("type", "text"),
                options=json.dumps(q["options"]) if q.get("options") else None,
                is_custom=q["_is_custom"],
            )
        )


@router.get("/survey/{token}")
async def survey_page(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    session = await _get_session(db, token)
    if not session or session.status == SurveyStatus.EXPIRED.value:
        return render(request, "survey/expired.html")

    if session.status == SurveyStatus.COMPLETED.value:
        return render(request, "survey/complete.html", session=session)

    if session.status == SurveyStatus.PENDING.value:
        return render(request, "survey/intro.html", session=session)

    unanswered = [q for q in session.questions if q.response is None]
    if not unanswered:
        return render(request, "survey/complete.html", session=session)

    return render(request, "survey/questions.html", session=session, questions=unanswered)


@router.post("/survey/{token}/respond")
async def respond(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    session = await _get_session(db, token)
    if not session or session.status in (SurveyStatus.EXPIRED.value, SurveyStatus.COMPLETED.value):
        return RedirectResponse(f"/survey/{token}", status_code=303)

    form = await request.form()
    if not verify_csrf_token(request, form.get("csrf_token")):
        return RedirectResponse(f"/survey/{token}", status_code=303)

    employee_dict = {
        "name": session.employee.name,
        "role": session.employee.role,
        "project": session.employee.project,
        "experience_years": session.employee.experience_years,
    }

    if session.status == SurveyStatus.PENDING.value:
        session.status = SurveyStatus.IN_PROGRESS.value
        session.current_round = 1
        await _generate_next_batch(session, db, employee_dict, [])
        await db.commit()
        return RedirectResponse(f"/survey/{token}", status_code=303)

    # Save answers for current unanswered questions
    unanswered = [q for q in session.questions if q.response is None]
    answered_now = {}
    for q in unanswered:
        answer = form.get(f"q_{q.id}")
        if answer and str(answer).strip():
            db.add(Response(question_id=q.id, session_id=session.id, answer_text=str(answer).strip()))
            answered_now[q.id] = str(answer).strip()

    await db.flush()

    total_questions = len(session.questions)
    if MAX_QUESTIONS - total_questions <= 0:
        session.status = SurveyStatus.COMPLETED.value
    else:
        prior_qa = []
        for q in session.questions:
            if q.response:
                prior_qa.append({"question": q.question_text, "answer": q.response.answer_text, "type": q.question_type})
            elif q.id in answered_now:
                prior_qa.append({"question": q.question_text, "answer": answered_now[q.id], "type": q.question_type})

        session.current_round += 1
        await _generate_next_batch(session, db, employee_dict, prior_qa)

    await db.commit()
    return RedirectResponse(f"/survey/{token}", status_code=303)
