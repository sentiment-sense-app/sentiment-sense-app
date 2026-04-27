from __future__ import annotations

import csv
import io
import secrets
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Employee, OnboardingToken


REQUIRED_COLUMNS = ["name", "email", "department", "manager", "project", "role", "phone"]


@dataclass
class ImportResult:
    imported: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def generate_onboarding_token() -> str:
    return f"onb_{secrets.token_urlsafe(18)}"


async def ensure_onboarding_token(db: AsyncSession, employee: Employee) -> OnboardingToken:
    token = (
        await db.execute(
            select(OnboardingToken)
            .where(OnboardingToken.employee_id == employee.id)
            .where(OnboardingToken.is_revoked.is_(False))
            .where(OnboardingToken.used_at.is_(None))
            .order_by(OnboardingToken.created_at.desc())
        )
    ).scalar_one_or_none()
    if token:
        return token
    token = OnboardingToken(employee_id=employee.id, token=generate_onboarding_token())
    db.add(token)
    await db.flush()
    return token


async def regenerate_onboarding_token(db: AsyncSession, employee: Employee) -> OnboardingToken:
    tokens = (
        await db.execute(
            select(OnboardingToken)
            .where(OnboardingToken.employee_id == employee.id)
            .where(OnboardingToken.used_at.is_(None))
            .where(OnboardingToken.is_revoked.is_(False))
        )
    ).scalars().all()
    for token in tokens:
        token.is_revoked = True
        db.add(token)
    new_token = OnboardingToken(employee_id=employee.id, token=generate_onboarding_token())
    db.add(new_token)
    await db.commit()
    await db.refresh(new_token)
    return new_token


async def import_employees_from_csv(db: AsyncSession, content: bytes) -> ImportResult:
    result = ImportResult()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        result.failed += 1
        result.errors.append("CSV must be UTF-8 encoded.")
        return result

    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    normalized_headers = [header.strip().lower() for header in headers]
    missing = [column for column in REQUIRED_COLUMNS if column not in normalized_headers]
    if missing:
        result.failed += 1
        result.errors.append(f"Missing required columns: {', '.join(missing)}")
        return result
    reader.fieldnames = normalized_headers

    seen_emails: set[str] = set()
    for row_number, row in enumerate(reader, start=2):
        normalized = {key.strip().lower(): (value or "").strip() for key, value in row.items() if key}
        email = normalized.get("email", "").lower()
        row_errors = []
        for column in REQUIRED_COLUMNS:
            if not normalized.get(column):
                row_errors.append(f"{column} is required")
        if email in seen_emails:
            row_errors.append("duplicate email in CSV")
        if row_errors:
            result.failed += 1
            result.errors.append(f"Row {row_number}: {', '.join(row_errors)}")
            continue

        seen_emails.add(email)
        employee = (await db.execute(select(Employee).where(Employee.email == email))).scalar_one_or_none()
        values = {column: normalized[column] for column in REQUIRED_COLUMNS}
        values["email"] = email
        if employee:
            for key, value in values.items():
                setattr(employee, key, value)
            result.updated += 1
        else:
            employee = Employee(**values)
            db.add(employee)
            result.imported += 1
        await db.flush()
        await ensure_onboarding_token(db, employee)

    await db.commit()
    return result
