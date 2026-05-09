import csv
import io
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.focus_areas import focus_labels
from app.models import Report, Survey, sentiment_band


REPORT_STATUSES = ["open", "reviewed", "resolved", "dismissed"]


def report_status_label(status: str) -> str:
    return status.replace("_", " ").title()


async def export_reports_csv(db: AsyncSession, status: str | None = None) -> str:
    query = select(Report, Survey).join(Survey, Survey.id == Report.survey_id).order_by(Report.created_at.desc())
    if status:
        query = query.where(Report.status == status)
    rows = (await db.execute(query)).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "report_id",
            "employee_name",
            "employee_email",
            "department",
            "manager",
            "project",
            "role",
            "phone",
            "sentiment_score",
            "sentiment_band",
            "focus_areas",
            "report",
            "status",
            "hr_notes",
            "created_at",
            "updated_at",
        ]
    )
    for report, survey in rows:
        employee = report.employee
        try:
            focus_slugs = json.loads(survey.focus_areas_json or "[]")
        except (ValueError, TypeError):
            focus_slugs = []
        writer.writerow(
            [
                report.id,
                employee.name,
                employee.email,
                employee.department,
                employee.manager,
                employee.project,
                employee.role,
                employee.phone,
                "" if report.sentiment_score is None else report.sentiment_score,
                sentiment_band(report.sentiment_score) or "",
                ", ".join(focus_labels(focus_slugs)),
                report.report_markdown,
                report.status,
                report.hr_notes,
                report.created_at.isoformat(),
                report.updated_at.isoformat(),
            ]
        )
    return output.getvalue()
