from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_admin_session, verify_csrf
from app.database import get_db
from app.models import AdminSession, Message, Report
from app.reports import REPORT_STATUSES, export_reports_csv
from app.web import templates


router = APIRouter(prefix="/admin")


@router.get("/surveys.csv")
async def surveys_csv(
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
    _session: AdminSession = Depends(require_admin_session),
) -> Response:
    selected_status = status if status in REPORT_STATUSES else None
    csv_content = await export_reports_csv(db, selected_status)
    return PlainTextResponse(
        csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=surveys.csv"},
    )


@router.get("/surveys/{report_id}", response_class=HTMLResponse)
async def survey_detail(
    report_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    session: AdminSession = Depends(require_admin_session),
) -> HTMLResponse:
    report = await db.get(Report, report_id)
    if not report:
        return templates.TemplateResponse(request, "404.html", status_code=404)
    messages = (
        await db.execute(
            select(Message).where(Message.survey_id == report.survey_id).order_by(Message.created_at.asc())
        )
    ).scalars().all()
    return templates.TemplateResponse(
        request,
        "report_detail.html",
        {
            "admin": session.admin,
            "csrf_token": session.csrf_token,
            "report": report,
            "messages": messages,
            "report_statuses": REPORT_STATUSES,
        },
    )


@router.post("/surveys/{report_id}")
async def update_survey(
    report_id: int,
    csrf_token: str = Form(...),
    status: str = Form(...),
    hr_notes: str = Form(""),
    db: AsyncSession = Depends(get_db),
    session: AdminSession = Depends(require_admin_session),
) -> RedirectResponse:
    verify_csrf(session, csrf_token)
    report = await db.get(Report, report_id)
    if not report:
        return RedirectResponse("/admin?error=Survey+not+found.", status_code=303)
    if status not in REPORT_STATUSES:
        return RedirectResponse(f"/admin/surveys/{report.id}?error=Invalid+status.", status_code=303)
    report.status = status
    report.hr_notes = hr_notes.strip()
    db.add(report)
    await db.commit()
    return RedirectResponse(f"/admin/surveys/{report.id}?notice=Survey+updated.", status_code=303)
