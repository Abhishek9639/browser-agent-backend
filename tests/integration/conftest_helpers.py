"""Builders shared by the repository tests."""

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import ActionType, UserRole
from app.models.task import Task, TaskStep
from app.models.user import RefreshToken, User


async def make_user(
    session: AsyncSession,
    email: str = "owner@example.test",
    role: UserRole = UserRole.TASK_SUBMITTER,
) -> User:
    user = User(name="Owner", email=email, password_hash="argon2-digest", role=role)
    session.add(user)
    await session.flush()
    return user


async def make_task(
    session: AsyncSession,
    user: User,
    instruction: str = "find the cheapest flight",
    **kwargs: object,
) -> Task:
    task = Task(instruction=instruction, created_by=user.id, **kwargs)
    session.add(task)
    await session.flush()
    await session.refresh(task)
    return task


async def make_step(
    session: AsyncSession,
    task: Task,
    step_number: int,
    action: ActionType = ActionType.CLICK,
) -> TaskStep:
    step = TaskStep(task_id=task.id, step_number=step_number, action=action)
    session.add(step)
    await session.flush()
    await session.refresh(step)
    return step


async def make_refresh_token(
    session: AsyncSession,
    user: User,
    token_hash: str,
    *,
    expires_in_days: int = 7,
) -> RefreshToken:
    token = RefreshToken(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=datetime.now(UTC) + timedelta(days=expires_in_days),
    )
    session.add(token)
    await session.flush()
    await session.refresh(token)
    return token
