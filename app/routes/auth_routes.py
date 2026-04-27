from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    clear_session_cookie,
    create_admin_session,
    require_admin_session,
    revoke_session,
    set_session_cookie,
    verify_csrf,
    verify_password,
)
from app.database import get_db
from app.models import Admin, AdminSession
from app.web import templates


router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("login.html", {"request": request})


@router.post("/login", response_model=None)
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse | HTMLResponse:
    admin = (await db.execute(select(Admin).where(Admin.email == email.strip().lower()))).scalar_one_or_none()
    if not admin or not admin.is_active or not verify_password(password, admin.password_hash):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Invalid email or password."},
            status_code=400,
        )

    raw_token, _session = await create_admin_session(db, admin, request)
    response = RedirectResponse("/admin", status_code=303)
    set_session_cookie(response, raw_token)
    return response


@router.post("/logout")
async def logout(
    csrf_token: str = Form(...),
    db: AsyncSession = Depends(get_db),
    session: AdminSession = Depends(require_admin_session),
) -> RedirectResponse:
    verify_csrf(session, csrf_token)
    await revoke_session(db, session)
    response = RedirectResponse("/login", status_code=303)
    clear_session_cookie(response)
    return response
