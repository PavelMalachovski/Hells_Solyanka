"""
database.py — SQLAlchemy 2.0 async models and DB connection helpers.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import AsyncGenerator

from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

DATABASE_URL: str = os.environ["DATABASE_URL"]
# Railway gives a postgres:// URL; asyncpg needs postgresql+asyncpg://
DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)

engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Question(Base):
    __tablename__ = "questions"
    __table_args__ = (UniqueConstraint("link", name="uq_question_link"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pack_name: Mapped[str] = mapped_column(String(256), nullable=False)
    pack_link: Mapped[str] = mapped_column(String(512), nullable=False)
    question_number: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    link: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    is_sent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


# ---------- helpers ----------

async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency / context manager yielding an async DB session."""
    async with async_session_factory() as session:
        yield session


async def init_db() -> None:
    """Create tables if they don't exist yet."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def upsert_questions(questions: list[dict]) -> int:
    """
    Insert questions that don't exist yet (keyed on `link`).
    Returns number of newly inserted rows.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    if not questions:
        return 0

    stmt = pg_insert(Question).values(questions)
    stmt = stmt.on_conflict_do_nothing(index_elements=["link"])

    async with async_session_factory() as session:
        result = await session.execute(stmt)
        await session.commit()
        return result.rowcount


async def get_random_unsent() -> Question | None:
    """Pick a random question that hasn't been sent yet."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(Question)
            .where(Question.is_sent == False)  # noqa: E712
            .order_by(func.random())
            .limit(1)
        )
        return result.scalar_one_or_none()


async def mark_as_sent(question_id: int) -> None:
    async with async_session_factory() as session:
        q = await session.get(Question, question_id)
        if q:
            q.is_sent = True
            await session.commit()


async def count_questions(unsent_only: bool = False) -> int:
    async with async_session_factory() as session:
        stmt = select(func.count()).select_from(Question)
        if unsent_only:
            stmt = stmt.where(Question.is_sent == False)  # noqa: E712
        result = await session.execute(stmt)
        return result.scalar_one()
