from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_admin_session, verify_csrf
from app.bot_logic import (
    latest_report_for_employee,
    survey_label,
)
from app.config import settings
from app.csv_import import REQUIRED_COLUMNS as EMPLOYEE_FIELDS, ensure_onboarding_token, import_employees_from_csv, regenerate_onboarding_token
from app.database import get_db
from app.models import AdminSession, Employee, Message, OnboardingToken, Report, Survey
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
        "survey_label": await survey_label(db, employee.id),
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
        request,
        "employees.html",
        {
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
        request,
        "import_employees.html",
        {"admin": session.admin, "csrf_token": session.csrf_token},
    )


@router.get("/employees/new", response_class=HTMLResponse)
async def new_employee_form(
    request: Request,
    session: AdminSession = Depends(require_admin_session),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "add_employee.html",
        {
            "admin": session.admin,
            "csrf_token": session.csrf_token,
            "values": {column: "" for column in EMPLOYEE_FIELDS},
            "errors": [],
        },
    )


@router.post("/employees/new")
async def create_employee(
    request: Request,
    csrf_token: str = Form(...),
    name: str = Form(""),
    email: str = Form(""),
    department: str = Form(""),
    manager: str = Form(""),
    project: str = Form(""),
    role: str = Form(""),
    phone: str = Form(""),
    db: AsyncSession = Depends(get_db),
    session: AdminSession = Depends(require_admin_session),
):
    verify_csrf(session, csrf_token)
    values = {
        "name": name.strip(),
        "email": email.strip().lower(),
        "department": department.strip(),
        "manager": manager.strip(),
        "project": project.strip(),
        "role": role.strip(),
        "phone": phone.strip(),
    }
    errors = [f"{field} is required" for field in EMPLOYEE_FIELDS if not values[field]]
    if values["email"] and not errors:
        existing = (await db.execute(select(Employee).where(Employee.email == values["email"]))).scalar_one_or_none()
        if existing:
            errors.append("an employee with this email already exists")

    if errors:
        return templates.TemplateResponse(
            request,
            "add_employee.html",
            {
                "admin": session.admin,
                "csrf_token": session.csrf_token,
                "values": values,
                "errors": errors,
            },
            status_code=400,
        )

    employee = Employee(**values)
    db.add(employee)
    await db.flush()
    await ensure_onboarding_token(db, employee)
    await db.commit()
    return redirect_to(f"/admin/employees/{employee.id}", notice="Employee added.")


@router.post("/employees/import")
async def import_employees(
    csrf_token: str = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    session: AdminSession = Depends(require_admin_session),
) -> RedirectResponse:
    verify_csrf(session, csrf_token)
    content = await file.read()
    result = await import_employees_from_csv(db, content)
    parts = [f"Imported {result.imported}", f"updated {result.updated}"]
    if result.failed:
        parts.append(f"failed {result.failed}")
    notice = ", ".join(parts) + "."
    error = "; ".join(result.errors) if result.errors else None
    return redirect_to("/admin/employees", notice=notice, error=error)


@router.get("/employees/{employee_id}", response_class=HTMLResponse)
async def employee_detail(
    employee_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    session: AdminSession = Depends(require_admin_session),
) -> HTMLResponse:
    employee = await db.get(Employee, employee_id)
    if not employee:
        return templates.TemplateResponse(request, "404.html", status_code=404)
    token = await ensure_onboarding_token(db, employee)
    latest_report = await latest_report_for_employee(db, employee.id)
    label = await survey_label(db, employee.id)
    await db.commit()
    return templates.TemplateResponse(
        request,
        "employee_detail.html",
        {
            "admin": session.admin,
            "csrf_token": session.csrf_token,
            "employee": employee,
            "deep_link": deep_link_for_token(token.token),
            "survey_label": label,
            "latest_report": latest_report,
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


@router.post("/employees/{employee_id}/delete")
async def delete_employee(
    employee_id: int,
    csrf_token: str = Form(...),
    db: AsyncSession = Depends(get_db),
    session: AdminSession = Depends(require_admin_session),
) -> RedirectResponse:
    verify_csrf(session, csrf_token)
    employee = await db.get(Employee, employee_id)
    if not employee:
        return redirect_to("/admin/employees", error="Employee not found.")
    name = employee.name
    await db.execute(delete(Report).where(Report.employee_id == employee_id))
    await db.execute(delete(Message).where(Message.employee_id == employee_id))
    await db.execute(delete(Survey).where(Survey.employee_id == employee_id))
    await db.execute(delete(OnboardingToken).where(OnboardingToken.employee_id == employee_id))
    await db.delete(employee)
    await db.commit()
    return redirect_to("/admin/employees", notice=f"Deleted {name} and all related survey data.")


