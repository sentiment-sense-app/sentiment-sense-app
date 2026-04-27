import hashlib
import secrets
from datetime import timedelta

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Admin, AdminSession, now_utc


password_hash = PasswordHash.recommended()


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        return password_hash.verify(password, stored_hash)
    except Exception:
        return False


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def seed_admin(db: AsyncSession) -> None:
    existing = (await db.execute(select(Admin).limit(1))).scalar_one_or_none()
    if existing:
        return
    admin = Admin(
        email=settings.admin_email.strip().lower(),
        password_hash=hash_password(settings.admin_password),
        is_active=True,
    )
    db.add(admin)
    await db.commit()


async def create_admin_session(db: AsyncSession, admin: Admin, request: Request) -> tuple[str, AdminSession]:
    raw_token = secrets.token_urlsafe(48)
    csrf_token = secrets.token_urlsafe(32)
    session = AdminSession(
        admin_id=admin.id,
        session_token_hash=hash_session_token(raw_token),
        csrf_token=csrf_token,
        expires_at=now_utc() + timedelta(hours=settings.session_ttl_hours),
        user_agent=request.headers.get("user-agent"),
        ip_address=request.client.host if request.client else None,
    )
    admin.last_login_at = now_utc()
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return raw_token, session


def set_session_cookie(response: RedirectResponse, token: str) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
        max_age=settings.session_ttl_hours * 3600,
    )


def clear_session_cookie(response: RedirectResponse) -> None:
    response.delete_cookie(settings.session_cookie_name, path="/")


async def revoke_session(db: AsyncSession, session: AdminSession) -> None:
    session.revoked_at = now_utc()
    db.add(session)
    await db.commit()


async def require_admin_session(request: Request, db: AsyncSession = Depends(get_db)) -> AdminSession:
    raw_token = request.cookies.get(settings.session_cookie_name)
    if not raw_token:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    token_hash = hash_session_token(raw_token)
    session = (await db.execute(select(AdminSession).where(AdminSession.session_token_hash == token_hash))).scalar_one_or_none()
    if not session or session.revoked_at or session.expires_at <= now_utc():
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    if not session.admin or not session.admin.is_active:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    request.state.admin_session = session
    request.state.admin = session.admin
    return session


def verify_csrf(session: AdminSession, csrf_token: str) -> None:
    if not secrets.compare_digest(session.csrf_token, csrf_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")
