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
# Normalise to the asyncpg dialect regardless of what Railway provides:
#   postgres://...        -> postgresql+asyncpg://...
#   postgresql://...      -> postgresql+asyncpg://...
#   postgresql+asyncpg:// -> unchanged
if "+" not in DATABASE_URL.split("://")[0]:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
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
    image_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
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
    """Create tables if they don't exist yet, and apply lightweight migrations."""
    from sqlalchemy import text
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Idempotent migration: add image_url if upgrading from older schema
        await conn.execute(text(
            "ALTER TABLE questions ADD COLUMN IF NOT EXISTS image_url VARCHAR(1024)"
        ))


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


PAGE_SIZE = 5


async def get_questions_paged(
    page: int = 0,
    unsent_only: bool = True,
) -> list[Question]:
    """Return PAGE_SIZE questions for the given page (0-indexed)."""
    async with async_session_factory() as session:
        stmt = select(Question).order_by(Question.id)
        if unsent_only:
            stmt = stmt.where(Question.is_sent == False)  # noqa: E712
        stmt = stmt.offset(page * PAGE_SIZE).limit(PAGE_SIZE)
        result = await session.execute(stmt)
        return list(result.scalars().all())


# ── Pack-level helpers ────────────────────────────────────────────────────────

async def get_packs(unsent_only: bool = False) -> list[dict]:
    """
    Return distinct packs ordered by pack_link, with question counts.
    Each dict: {pack_name, pack_link, pack_id, total, unsent}
    pack_id is extracted as the numeric suffix of pack_link.
    """
    import re as _re
    async with async_session_factory() as session:
        rows = await session.execute(
            select(
                Question.pack_name,
                Question.pack_link,
                func.count().label("total"),
                func.sum(
                    func.cast(Question.is_sent == False, Integer)  # noqa: E712
                ).label("unsent"),
            )
            .group_by(Question.pack_name, Question.pack_link)
            .order_by(Question.pack_link)
        )
        packs = []
        for row in rows:
            m = _re.search(r'/(\d+)$', row.pack_link or "")
            pack_id = m.group(1) if m else row.pack_link
            packs.append({
                "pack_name": row.pack_name,
                "pack_link": row.pack_link,
                "pack_id": pack_id,
                "total": row.total or 0,
                "unsent": int(row.unsent or 0),
            })
        return packs


async def get_questions_by_pack(
    pack_link: str,
    page: int = 0,
    unsent_only: bool = True,
) -> list[Question]:
    async with async_session_factory() as session:
        stmt = (
            select(Question)
            .where(Question.pack_link == pack_link)
            .order_by(Question.question_number)
        )
        if unsent_only:
            stmt = stmt.where(Question.is_sent == False)  # noqa: E712
        stmt = stmt.offset(page * PAGE_SIZE).limit(PAGE_SIZE)
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def count_questions_by_pack(
    pack_link: str,
    unsent_only: bool = True,
) -> int:
    async with async_session_factory() as session:
        stmt = (
            select(func.count())
            .select_from(Question)
            .where(Question.pack_link == pack_link)
        )
        if unsent_only:
            stmt = stmt.where(Question.is_sent == False)  # noqa: E712
        result = await session.execute(stmt)
        return result.scalar_one()


async def get_adjacent_in_pack(
    question_id: int,
    pack_link: str,
    unsent_only: bool = True,
) -> tuple[int | None, int | None]:
    """Return (prev_id, next_id) within the same pack, ordered by question_number."""
    async with async_session_factory() as session:
        q_now = await session.get(Question, question_id)
        if q_now is None:
            return None, None
        qnum = q_now.question_number

        def _base():
            s = (
                select(Question.id)
                .where(Question.pack_link == pack_link)
                .order_by(Question.question_number)
            )
            if unsent_only:
                s = s.where(Question.is_sent == False)  # noqa: E712
            return s

        prev_r = await session.execute(
            _base().where(Question.question_number < qnum)
            .order_by(Question.question_number.desc()).limit(1)
        )
        next_r = await session.execute(
            _base().where(Question.question_number > qnum).limit(1)
        )
        return prev_r.scalar_one_or_none(), next_r.scalar_one_or_none()


async def get_question_by_id(question_id: int) -> Question | None:
    async with async_session_factory() as session:
        return await session.get(Question, question_id)


async def get_adjacent_question_ids(
    question_id: int,
    unsent_only: bool = True,
) -> tuple[int | None, int | None]:
    """Return (prev_id, next_id) for a question, ordered by id."""
    async with async_session_factory() as session:
        def _base():
            q = select(Question.id).order_by(Question.id)
            if unsent_only:
                q = q.where(Question.is_sent == False)  # noqa: E712
            return q

        prev_result = await session.execute(
            _base().where(Question.id < question_id).order_by(Question.id.desc()).limit(1)
        )
        next_result = await session.execute(
            _base().where(Question.id > question_id).limit(1)
        )
        return prev_result.scalar_one_or_none(), next_result.scalar_one_or_none()


async def clear_questions() -> int:
    """Delete all questions. Returns the number of rows deleted."""
    from sqlalchemy import delete
    async with async_session_factory() as session:
        result = await session.execute(delete(Question))
        await session.commit()
        return result.rowcount
