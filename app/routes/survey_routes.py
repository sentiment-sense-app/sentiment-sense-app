from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_admin_session, verify_csrf
from app.bot_logic import start_survey_for_employee
from app.database import get_db
from app.models import AdminSession, Employee
from app.telegram_client import TelegramClient
from app.web import templates


router = APIRouter(prefix="/admin")


def redirect_to(path: str, notice: str | None = None, error: str | None = None) -> RedirectResponse:
    params = {}
    if notice:
        params["notice"] = notice
    if error:
        params["error"] = error
    suffix = f"?{urlencode(params)}" if params else ""
    return RedirectResponse(f"{path}{suffix}", status_code=303)


def parse_questions_text(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def parse_questions_csv(content: bytes) -> list[str]:
    if not content:
        return []
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return []
    questions = []
    for raw in text.splitlines():
        first = raw.split(",", 1)[0].strip().strip('"').strip("'")
        if not first:
            continue
        if first.lower() in {"question", "questions"} and not questions:
            continue
        questions.append(first)
    return questions


@router.get("/employees/{employee_id}/start-survey", response_class=HTMLResponse)
async def start_survey_form(
    employee_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    session: AdminSession = Depends(require_admin_session),
) -> HTMLResponse:
    employee = await db.get(Employee, employee_id)
    if not employee:
        return templates.TemplateResponse(request, "404.html", status_code=404)
    return templates.TemplateResponse(
        request,
        "start_survey.html",
        {
            "admin": session.admin,
            "csrf_token": session.csrf_token,
            "employee": employee,
        },
    )


@router.post("/employees/{employee_id}/start-survey")
async def start_survey_submit(
    employee_id: int,
    csrf_token: str = Form(...),
    total_questions: int = Form(3),
    custom_percent: int = Form(0),
    custom_questions_text: str = Form(""),
    custom_questions_file: UploadFile | None = File(None),
    db: AsyncSession = Depends(get_db),
    session: AdminSession = Depends(require_admin_session),
) -> RedirectResponse:
    verify_csrf(session, csrf_token)
    employee = await db.get(Employee, employee_id)
    if not employee:
        return redirect_to("/admin/employees", error="Employee not found.")

    total_questions = max(1, min(20, total_questions))
    custom_percent = max(0, min(100, custom_percent))

    customs = parse_questions_text(custom_questions_text)
    if custom_questions_file is not None and custom_questions_file.filename:
        customs.extend(parse_questions_csv(await custom_questions_file.read()))

    ok, message = await start_survey_for_employee(
        db,
        TelegramClient(),
        employee,
        created_by_admin_id=session.admin_id,
        cancel_existing=True,
        total_questions=total_questions,
        custom_questions=customs,
        custom_percent=custom_percent,
    )
    return redirect_to(f"/admin/employees/{employee.id}", notice=message if ok else None, error=None if ok else message)
