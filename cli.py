import asyncio
import sys
from getpass import getpass

from sqlalchemy import select

from app.core import hash_password
from app.database import async_session, init_db
from app.models import Admin


async def create_admin():
    await init_db()

    username = input("Username: ")
    password = getpass("Password: ")

    if len(password) < 6:
        print("Password must be at least 6 characters.")
        sys.exit(1)

    async with async_session() as db:
        existing = await db.execute(select(Admin).where(Admin.username == username))
        if existing.scalar_one_or_none():
            print(f"Admin '{username}' already exists.")
            sys.exit(1)

        db.add(Admin(username=username, password_hash=hash_password(password)))
        await db.commit()

    print(f"Admin '{username}' created.")


if __name__ == "__main__":
    asyncio.run(create_admin())
