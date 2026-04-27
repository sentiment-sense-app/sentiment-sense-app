from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_admin_session, verify_csrf
from app.bot_logic import start_checkin_for_employee
from app.database import get_db
from app.models import AdminSession, Employee
from app.telegram_client import TelegramClient


router = APIRouter(prefix="/admin")


def redirect_to(path: str, notice: str | None = None, error: str | None = None) -> RedirectResponse:
    params = {}
    if notice:
        params["notice"] = notice
    if error:
        params["error"] = error
    suffix = f"?{urlencode(params)}" if params else ""
    return RedirectResponse(f"{path}{suffix}", status_code=303)


@router.post("/employees/{employee_id}/send-checkin")
async def send_employee_checkin(
    employee_id: int,
    csrf_token: str = Form(...),
    db: AsyncSession = Depends(get_db),
    session: AdminSession = Depends(require_admin_session),
) -> RedirectResponse:
    verify_csrf(session, csrf_token)
    employee = await db.get(Employee, employee_id)
    if not employee:
        return redirect_to("/admin/employees", error="Employee not found.")
    ok, message = await start_checkin_for_employee(
        db, TelegramClient(), employee, created_by_admin_id=session.admin_id, cancel_existing=True
    )
    return redirect_to(f"/admin/employees/{employee.id}", notice=message if ok else None, error=None if ok else message)
