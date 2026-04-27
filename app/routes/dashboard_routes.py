from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_admin_session
from app.database import get_db
from app.models import AdminSession, CheckinSession, Employee, Report
from app.web import templates


router = APIRouter()


@router.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse("/admin", status_code=303)


@router.get("/admin", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),
    session: AdminSession = Depends(require_admin_session),
) -> HTMLResponse:
    stats = {
        "total_employees": (await db.execute(select(func.count(Employee.id)))).scalar() or 0,
        "active_checkins": (await db.execute(select(func.count(CheckinSession.id)).where(CheckinSession.status == "active"))).scalar() or 0,
        "completed_checkins": (await db.execute(select(func.count(CheckinSession.id)).where(CheckinSession.status == "completed"))).scalar() or 0,
        "reports_generated": (await db.execute(select(func.count(Report.id)))).scalar() or 0,
        "open_reports": (await db.execute(select(func.count(Report.id)).where(Report.status == "open"))).scalar() or 0,
    }
    latest_reports = (
        await db.execute(select(Report).order_by(Report.created_at.desc()).limit(8))
    ).scalars().all()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "admin": session.admin,
            "csrf_token": session.csrf_token,
            "stats": stats,
            "latest_reports": latest_reports,
        },
    )
