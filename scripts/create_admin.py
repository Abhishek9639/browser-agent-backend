#!/usr/bin/env python3
"""Create an ADMIN or APPROVER account from the command line.

    python scripts/create_admin.py --email you@example.com --name "Your Name" --role ADMIN

The password is prompted for by default rather than taken as an argument: an
argument ends up in shell history and in the process list, where anyone on the
box can read it. ``--password`` exists for scripted setup and says so.

Roles above TASK_SUBMITTER are not self-service, which is why this is a script
and not an endpoint.
"""

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings
from app.core.security import MIN_PASSWORD_LENGTH, hash_password
from app.db.session import Database
from app.domain.enums import UserRole
from app.models.user import User
from app.repositories.audit import AuditEventRepository
from app.repositories.user import UserRepository
from app.services.audit import AuditAction, AuditService

ALLOWED_ROLES = (UserRole.ADMIN, UserRole.APPROVER)


def read_password(supplied: str | None) -> str:
    if supplied:
        return supplied
    first = getpass.getpass("Password: ")
    second = getpass.getpass("Repeat: ")
    if first != second:
        raise SystemExit("Passwords do not match.")
    return first


def validate(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise SystemExit(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if password.isdigit():
        raise SystemExit("Password cannot be all digits.")


async def create(*, name: str, email: str, password: str, role: UserRole) -> None:
    settings = get_settings()
    database = Database(settings)
    try:
        async with database.session() as session:
            users = UserRepository(session)
            if await users.email_exists(email):
                raise SystemExit(f"{email} is already registered.")

            user = await users.add(
                User(
                    name=name,
                    email=email,
                    password_hash=hash_password(password),
                    role=role,
                )
            )
            await AuditService(AuditEventRepository(session)).record(
                AuditAction.USER_REGISTERED,
                actor_id=user.id,
                metadata={"email": email, "role": role.value, "created_by": "cli"},
            )
        print(f"Created {role.value} {email} ({user.id})")  # noqa: T201
    finally:
        await database.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument(
        "--role",
        default=UserRole.ADMIN.value,
        choices=[role.value for role in ALLOWED_ROLES],
    )
    parser.add_argument(
        "--password",
        help="Prefer the prompt; an argument is visible in shell history and ps.",
    )
    args = parser.parse_args()

    email = args.email.strip().lower()
    name = args.name.strip()
    if not name:
        raise SystemExit("Name cannot be blank.")

    password = read_password(args.password)
    validate(password)

    asyncio.run(create(name=name, email=email, password=password, role=UserRole(args.role)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
