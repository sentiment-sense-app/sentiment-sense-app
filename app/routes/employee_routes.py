from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_admin_session, verify_csrf
from app.bot_logic import (
    active_session_for_employee,
    checkin_label,
    latest_report_for_employee,
    latest_session_for_employee,
)
from app.config import settings
from app.csv_import import ensure_onboarding_token, import_employees_from_csv, regenerate_onboarding_token
from app.database import get_db
from app.models import AdminSession, Employee, Message, now_utc
from app.reports import REPORT_STATUSES
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


def deep_link_for_token(token: str) -> str:
    if not settings.telegram_bot_url:
        return ""
    return f"{settings.telegram_bot_url}?start={token}"


async def employee_view_model(db: AsyncSession, employee: Employee) -> dict:
    token = await ensure_onboarding_token(db, employee)
    report = await latest_report_for_employee(db, employee.id)
    return {
        "employee": employee,
        "checkin_label": await checkin_label(db, employee.id),
        "latest_report": report,
        "deep_link": deep_link_for_token(token.token),
    }


@router.get("/employees", response_class=HTMLResponse)
async def employees(
    request: Request,
    db: AsyncSession = Depends(get_db),
    session: AdminSession = Depends(require_admin_session),
) -> HTMLResponse:
    employees_list = (await db.execute(select(Employee).order_by(Employee.name.asc()))).scalars().all()
    rows = [await employee_view_model(db, employee) for employee in employees_list]
    await db.commit()
    return templates.TemplateResponse(
        "employees.html",
        {
            "request": request,
            "admin": session.admin,
            "csrf_token": session.csrf_token,
            "rows": rows,
        },
    )


@router.get("/employees/import", response_class=HTMLResponse)
async def import_form(
    request: Request,
    session: AdminSession = Depends(require_admin_session),
) -> HTMLResponse:
    return templates.TemplateResponse(
        "import_employees.html",
        {"request": request, "admin": session.admin, "csrf_token": session.csrf_token},
    )


@router.post("/employees/import", response_class=HTMLResponse)
async def import_employees(
    request: Request,
    csrf_token: str = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    session: AdminSession = Depends(require_admin_session),
) -> HTMLResponse:
    verify_csrf(session, csrf_token)
    content = await file.read()
    result = await import_employees_from_csv(db, content)
    return templates.TemplateResponse(
        "import_employees.html",
        {
            "request": request,
            "admin": session.admin,
            "csrf_token": session.csrf_token,
            "result": result,
        },
    )


@router.get("/employees/{employee_id}", response_class=HTMLResponse)
async def employee_detail(
    employee_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    session: AdminSession = Depends(require_admin_session),
) -> HTMLResponse:
    employee = await db.get(Employee, employee_id)
    if not employee:
        return templates.TemplateResponse("404.html", {"request": request}, status_code=404)
    token = await ensure_onboarding_token(db, employee)
    latest_session = await latest_session_for_employee(db, employee.id)
    messages: list[Message] = []
    if latest_session:
        messages = list(
            (
                await db.execute(
                    select(Message)
                    .where(Message.session_id == latest_session.id)
                    .order_by(Message.created_at.asc())
                )
            ).scalars().all()
        )
    latest_report = await latest_report_for_employee(db, employee.id)
    label = await checkin_label(db, employee.id)
    await db.commit()
    return templates.TemplateResponse(
        "employee_detail.html",
        {
            "request": request,
            "admin": session.admin,
            "csrf_token": session.csrf_token,
            "employee": employee,
            "deep_link": deep_link_for_token(token.token),
            "checkin_label": label,
            "latest_session": latest_session,
            "messages": messages,
            "latest_report": latest_report,
            "report_statuses": REPORT_STATUSES,
        },
    )


@router.post("/employees/{employee_id}/regenerate-link")
async def regenerate_link(
    employee_id: int,
    csrf_token: str = Form(...),
    db: AsyncSession = Depends(get_db),
    session: AdminSession = Depends(require_admin_session),
) -> RedirectResponse:
    verify_csrf(session, csrf_token)
    employee = await db.get(Employee, employee_id)
    if not employee:
        return redirect_to("/admin/employees", error="Employee not found.")
    await regenerate_onboarding_token(db, employee)
    return redirect_to(f"/admin/employees/{employee.id}", notice="Deep link regenerated.")


@router.post("/employees/{employee_id}/reset")
async def reset_conversation(
    employee_id: int,
    csrf_token: str = Form(...),
    db: AsyncSession = Depends(get_db),
    session: AdminSession = Depends(require_admin_session),
) -> RedirectResponse:
    verify_csrf(session, csrf_token)
    employee = await db.get(Employee, employee_id)
    if not employee:
        return redirect_to("/admin/employees", error="Employee not found.")
    active = await active_session_for_employee(db, employee.id)
    if not active:
        return redirect_to(f"/admin/employees/{employee.id}", notice="No active conversation to reset.")
    active.status = "cancelled"
    active.cancelled_at = now_utc()
    db.add(active)
    await db.commit()
    return redirect_to(f"/admin/employees/{employee.id}", notice="Active conversation cancelled.")


@router.post("/employees/{employee_id}/report-status")
async def update_latest_report_status(
    employee_id: int,
    csrf_token: str = Form(...),
    status: str = Form(...),
    db: AsyncSession = Depends(get_db),
    session: AdminSession = Depends(require_admin_session),
) -> RedirectResponse:
    verify_csrf(session, csrf_token)
    employee = await db.get(Employee, employee_id)
    if not employee:
        return redirect_to("/admin/employees", error="Employee not found.")
    report = await latest_report_for_employee(db, employee.id)
    if not report:
        return redirect_to(f"/admin/employees/{employee.id}", error="No report exists for this employee.")
    if status not in REPORT_STATUSES:
        return redirect_to(f"/admin/employees/{employee.id}", error="Invalid report status.")
    report.status = status
    db.add(report)
    await db.commit()
    return redirect_to(f"/admin/employees/{employee.id}", notice="Report status updated.")
