import json
import secrets

import bcrypt
from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import BASE_DIR
from app.database import get_db
from app.models import Admin

limiter = Limiter(key_func=get_remote_address)

templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
templates.env.filters["from_json"] = json.loads


class RequiresLogin(Exception):
    pass


async def require_admin(request: Request, db: AsyncSession = Depends(get_db)) -> Admin:
    admin_id = request.session.get("admin_id")
    if not admin_id:
        raise RequiresLogin()
    admin = await db.get(Admin, admin_id)
    if not admin:
        request.session.clear()
        raise RequiresLogin()
    return admin


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


def get_csrf_token(request: Request) -> str:
    if "csrf_token" not in request.session:
        request.session["csrf_token"] = secrets.token_hex(32)
    return request.session["csrf_token"]


def verify_csrf_token(request: Request, token: str | None) -> bool:
    return token is not None and token == request.session.get("csrf_token")


def flash(request: Request, message: str, category: str = "info"):
    if "_messages" not in request.session:
        request.session["_messages"] = []
    request.session["_messages"].append({"message": message, "category": category})


def render(request: Request, template: str, status_code: int = 200, **context):
    context["csrf_token"] = get_csrf_token(request)
    context["messages"] = request.session.pop("_messages", [])
    return templates.TemplateResponse(request, template, context, status_code=status_code)
