import csv
import io

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Report


REPORT_STATUSES = ["open", "reviewed", "resolved", "dismissed"]


def report_status_label(status: str) -> str:
    return status.replace("_", " ").title()


async def export_reports_csv(db: AsyncSession, status: str | None = None) -> str:
    query = select(Report).order_by(Report.created_at.desc())
    if status:
        query = query.where(Report.status == status)
    reports = (await db.execute(query)).scalars().all()

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
            "report",
            "status",
            "hr_notes",
            "created_at",
            "updated_at",
        ]
    )
    for report in reports:
        employee = report.employee
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
                report.report_markdown,
                report.status,
                report.hr_notes,
                report.created_at.isoformat(),
                report.updated_at.isoformat(),
            ]
        )
    return output.getvalue()
