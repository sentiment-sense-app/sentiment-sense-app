from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_admin_session
from app.bot_logic import SURVEY_STATUSES
from app.database import get_db
from app.models import AdminSession, Employee, Report, Survey
from app.web import templates


router = APIRouter()

SURVEYS_PER_PAGE = 20
SORT_OPTIONS = {"recent": "Most recent", "priority": "Priority (Red first)"}


@router.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse("/admin", status_code=303)


@router.get("/admin/docs", response_class=HTMLResponse)
async def docs(
    request: Request,
    session: AdminSession = Depends(require_admin_session),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "docs.html",
        {"admin": session.admin, "csrf_token": session.csrf_token},
    )


@router.get("/admin", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    page: int = 1,
    status: str | None = None,
    sort: str | None = None,
    db: AsyncSession = Depends(get_db),
    session: AdminSession = Depends(require_admin_session),
) -> HTMLResponse:
    stats = {
        "total_employees": (await db.execute(select(func.count(Employee.id)))).scalar() or 0,
        "active_surveys": (await db.execute(select(func.count(Survey.id)).where(Survey.status == "active"))).scalar() or 0,
        "completed_surveys": (await db.execute(select(func.count(Survey.id)).where(Survey.status == "completed"))).scalar() or 0,
        "reports_generated": (await db.execute(select(func.count(Report.id)))).scalar() or 0,
        "open_reports": (await db.execute(select(func.count(Report.id)).where(Report.status == "open"))).scalar() or 0,
    }

    selected_status = status if status in SURVEY_STATUSES else None
    selected_sort = sort if sort in SORT_OPTIONS else "recent"
    page = max(page, 1)
    base_query = (
        select(Survey, Report)
        .outerjoin(Report, Report.survey_id == Survey.id)
    )
    if selected_sort == "priority":
        # Lowest score first (red→green); NULLs (no report yet) last; tie-break by recency.
        base_query = base_query.order_by(
            case((Report.sentiment_score.is_(None), 1), else_=0),
            Report.sentiment_score.asc(),
            Survey.created_at.desc(),
        )
    else:
        base_query = base_query.order_by(Survey.created_at.desc())
    count_query = select(func.count(Survey.id))
    if selected_status:
        base_query = base_query.where(Survey.status == selected_status)
        count_query = count_query.where(Survey.status == selected_status)
    total_surveys = (await db.execute(count_query)).scalar() or 0
    total_pages = max((total_surveys + SURVEYS_PER_PAGE - 1) // SURVEYS_PER_PAGE, 1)
    page = min(page, total_pages)
    rows = [
        {"survey": survey, "report": report}
        for survey, report in (
            await db.execute(base_query.offset((page - 1) * SURVEYS_PER_PAGE).limit(SURVEYS_PER_PAGE))
        ).all()
    ]

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "admin": session.admin,
            "csrf_token": session.csrf_token,
            "stats": stats,
            "rows": rows,
            "survey_statuses": SURVEY_STATUSES,
            "selected_status": selected_status,
            "sort_options": SORT_OPTIONS,
            "selected_sort": selected_sort,
            "page": page,
            "total_pages": total_pages,
            "total_surveys": total_surveys,
        },
    )
