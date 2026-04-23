import csv
import io
import json
import random
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import SURVEY_EXPIRY_DAYS
from app.core import flash, limiter, render, require_admin, verify_csrf_token, verify_password
from app.database import get_db
from app.models import Admin, Employee, Question, Response, SurveySession, SurveyStatus
from app.services.ai import cleanup_custom_questions

router = APIRouter()


@router.get("/login")
async def login_page(request: Request):
    if request.session.get("admin_id"):
        return RedirectResponse("/admin/dashboard", status_code=302)
    return render(request, "admin/login.html")


@router.post("/login")
@limiter.limit("5/minute")
async def login(
    request: Request,
    db: AsyncSession = Depends(get_db),
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
):
    if not verify_csrf_token(request, csrf_token):
        return render(request, "admin/login.html", error="Invalid request.", status_code=403)

    result = await db.execute(select(Admin).where(Admin.username == username))
    admin = result.scalar_one_or_none()

    if not admin or not verify_password(password, admin.password_hash):
        return render(request, "admin/login.html", error="Invalid credentials.", status_code=401)

    request.session["admin_id"] = admin.id
    return RedirectResponse("/admin/dashboard", status_code=303)


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=303)


PAGE_SIZE = 50


@router.get("/dashboard")
async def dashboard(
    request: Request,
    admin: Admin = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    surveys_page: int = 1,
    employees_page: int = 1,
):
    surveys_page = max(surveys_page, 1)
    employees_page = max(employees_page, 1)

    total_employees = (await db.execute(select(func.count()).select_from(Employee))).scalar_one()
    status_counts = dict(
        (await db.execute(select(SurveySession.status, func.count()).group_by(SurveySession.status))).all()
    )

    stats = {
        "total_employees": total_employees,
        "total_surveys": sum(status_counts.values()),
        "pending": status_counts.get(SurveyStatus.PENDING.value, 0),
        "in_progress": status_counts.get(SurveyStatus.IN_PROGRESS.value, 0),
        "completed": status_counts.get(SurveyStatus.COMPLETED.value, 0),
    }

    employees = (
        await db.execute(
            select(Employee)
            .order_by(Employee.id.desc())
            .limit(PAGE_SIZE)
            .offset((employees_page - 1) * PAGE_SIZE)
        )
    ).scalars().all()

    surveys = (
        await db.execute(
            select(SurveySession)
            .options(selectinload(SurveySession.employee))
            .order_by(SurveySession.created_at.desc())
            .limit(PAGE_SIZE)
            .offset((surveys_page - 1) * PAGE_SIZE)
        )
    ).scalars().all()

    def total_pages(count: int) -> int:
        return max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE)

    pagination = {
        "surveys": {"page": surveys_page, "total_pages": total_pages(stats["total_surveys"])},
        "employees": {"page": employees_page, "total_pages": total_pages(total_employees)},
    }

    return render(
        request,
        "admin/dashboard.html",
        admin=admin,
        employees=employees,
        surveys=surveys,
        stats=stats,
        pagination=pagination,
    )


@router.get("/employees/upload")
async def upload_page(request: Request, admin: Admin = Depends(require_admin)):
    return render(request, "admin/upload.html")


@router.post("/employees/upload")
async def upload_employees(
    request: Request,
    admin: Admin = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    file: UploadFile = File(...),
    csrf_token: str = Form(...),
):
    if not verify_csrf_token(request, csrf_token):
        flash(request, "Invalid request.", "error")
        return RedirectResponse("/admin/employees/upload", status_code=303)

    if not file.filename or not file.filename.endswith(".csv"):
        flash(request, "Please upload a CSV file.", "error")
        return RedirectResponse("/admin/employees/upload", status_code=303)

    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        flash(request, "File too large (max 5MB).", "error")
        return RedirectResponse("/admin/employees/upload", status_code=303)

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        flash(request, "Invalid file encoding. Use UTF-8.", "error")
        return RedirectResponse("/admin/employees/upload", status_code=303)

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames:
        reader.fieldnames = [h.strip().lower() for h in reader.fieldnames]
    if not reader.fieldnames or not {"name", "email", "role"}.issubset(set(reader.fieldnames)):
        flash(request, "CSV must have 'name', 'email', and 'role' columns.", "error")
        return RedirectResponse("/admin/employees/upload", status_code=303)

    created = 0
    skipped = 0

    for row in reader:
        name = row.get("name", "").strip()
        email = row.get("email", "").strip()
        role = row.get("role", "").strip()
        if not name or not email or not role:
            skipped += 1
            continue

        existing = await db.execute(select(Employee).where(Employee.email == email))
        if existing.scalar_one_or_none():
            skipped += 1
            continue

        exp = row.get("experience_years", "").strip()
        employee = Employee(
            name=name,
            email=email,
            role=role,
            project=row.get("project", "").strip() or None,
            experience_years=int(exp) if exp.isdigit() else None,
        )
        db.add(employee)
        await db.flush()

        db.add(
            SurveySession(
                employee_id=employee.id,
                token=str(uuid.uuid4()),
                status=SurveyStatus.PENDING.value,
                expires_at=datetime.now(timezone.utc) + timedelta(days=SURVEY_EXPIRY_DAYS),
            )
        )
        created += 1

    await db.commit()
    flash(request, f"Added {created} employees with surveys. Skipped {skipped} (duplicate/invalid).", "success")
    return RedirectResponse("/admin/dashboard", status_code=303)


@router.get("/surveys/create")
async def create_survey_page(
    request: Request,
    admin: Admin = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    employees = (await db.execute(select(Employee).order_by(Employee.name))).scalars().all()
    return render(request, "admin/create_survey.html", employees=employees)


def _normalize_texts(texts: list[str]) -> frozenset[str]:
    return frozenset(t.strip().lower() for t in texts if t and t.strip())


def _stored_text_set(custom_json: str | None) -> frozenset[str]:
    if not custom_json:
        return frozenset()
    try:
        return _normalize_texts([q.get("text", "") for q in json.loads(custom_json)])
    except (json.JSONDecodeError, AttributeError, TypeError):
        return frozenset()


@router.post("/surveys/create")
async def create_survey(
    request: Request,
    admin: Admin = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    if not verify_csrf_token(request, form.get("csrf_token")):
        flash(request, "Invalid request.", "error")
        return RedirectResponse("/admin/surveys/create", status_code=303)

    employee_ids = [int(e) for e in form.getlist("employee_ids")]
    focus_area = form.get("focus_area", "").strip()
    question_texts = [t.strip() for t in form.getlist("question_texts") if t.strip()]

    if not employee_ids:
        flash(request, "Select at least one employee.", "error")
        return RedirectResponse("/admin/surveys/create", status_code=303)

    new_focus = focus_area or ""
    new_text_set = _normalize_texts(question_texts)

    existing = (await db.execute(
        select(SurveySession.employee_id, SurveySession.focus_area, SurveySession.custom_questions)
        .where(
            SurveySession.employee_id.in_(employee_ids),
            SurveySession.status == SurveyStatus.PENDING.value,
        )
    )).all()

    dup_ids = {
        eid for eid, focus, custom in existing
        if (focus or "") == new_focus and _stored_text_set(custom) == new_text_set
    }
    to_create = [eid for eid in employee_ids if eid not in dup_ids]
    skipped = len(employee_ids) - len(to_create)

    if not to_create:
        flash(request, f"All {skipped} selected employee(s) already have an identical pending survey.", "error")
        return RedirectResponse("/admin/surveys/create", status_code=303)

    custom_json: str | None = None
    if question_texts:
        cleaned = await cleanup_custom_questions(question_texts)
        random.shuffle(cleaned)
        custom_json = json.dumps(cleaned)

    for eid in to_create:
        db.add(
            SurveySession(
                employee_id=eid,
                token=str(uuid.uuid4()),
                status=SurveyStatus.PENDING.value,
                focus_area=focus_area or None,
                custom_questions=custom_json,
                expires_at=datetime.now(timezone.utc) + timedelta(days=SURVEY_EXPIRY_DAYS),
            )
        )

    await db.commit()
    msg = f"Created {len(to_create)} survey(s)"
    if question_texts:
        msg += f" with {len(question_texts)} custom question(s)"
    if skipped:
        msg += f". Skipped {skipped} (identical pending survey already exists)"
    flash(request, msg + ".", "success")
    return RedirectResponse("/admin/dashboard", status_code=303)


@router.get("/surveys/{survey_id}/results")
async def survey_results(
    survey_id: int,
    request: Request,
    admin: Admin = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(SurveySession)
        .where(SurveySession.id == survey_id)
        .options(
            selectinload(SurveySession.employee),
            selectinload(SurveySession.questions).selectinload(Question.response),
        )
    )
    survey = result.scalar_one_or_none()
    if not survey:
        flash(request, "Survey not found.", "error")
        return RedirectResponse("/admin/dashboard", status_code=303)

    return render(request, "admin/results.html", survey=survey)


@router.post("/employees/{employee_id}/delete")
async def delete_employee(
    employee_id: int,
    request: Request,
    admin: Admin = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    csrf_token: str = Form(...),
):
    if not verify_csrf_token(request, csrf_token):
        flash(request, "Invalid request.", "error")
        return RedirectResponse("/admin/dashboard#employees", status_code=303)

    employee = (
        await db.execute(select(Employee).where(Employee.id == employee_id))
    ).scalar_one_or_none()
    if not employee:
        flash(request, "Employee not found.", "error")
        return RedirectResponse("/admin/dashboard#employees", status_code=303)

    session_ids = select(SurveySession.id).where(SurveySession.employee_id == employee_id)
    await db.execute(delete(Response).where(Response.session_id.in_(session_ids)))
    await db.execute(delete(Question).where(Question.session_id.in_(session_ids)))
    await db.execute(delete(SurveySession).where(SurveySession.employee_id == employee_id))
    await db.execute(delete(Employee).where(Employee.id == employee_id))
    await db.commit()

    flash(request, f"Deleted employee {employee.name} and all related surveys.", "success")
    return RedirectResponse("/admin/dashboard#employees", status_code=303)


@router.post("/surveys/{survey_id}/reset")
async def reset_survey(
    survey_id: int,
    request: Request,
    admin: Admin = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    csrf_token: str = Form(...),
):
    if not verify_csrf_token(request, csrf_token):
        flash(request, "Invalid request.", "error")
        return RedirectResponse("/admin/dashboard", status_code=303)

    survey = (
        await db.execute(
            select(SurveySession)
            .where(SurveySession.id == survey_id)
            .options(selectinload(SurveySession.employee))
        )
    ).scalar_one_or_none()
    if not survey:
        flash(request, "Survey not found.", "error")
        return RedirectResponse("/admin/dashboard", status_code=303)

    if survey.status == SurveyStatus.COMPLETED.value:
        flash(request, "Completed surveys cannot be reset.", "error")
        return RedirectResponse("/admin/dashboard", status_code=303)

    await db.execute(delete(Response).where(Response.session_id == survey_id))
    await db.execute(delete(Question).where(Question.session_id == survey_id))
    survey.status = SurveyStatus.PENDING.value
    survey.current_round = 0
    await db.commit()

    flash(request, f"Survey for {survey.employee.name} reset.", "success")
    return RedirectResponse("/admin/dashboard", status_code=303)
