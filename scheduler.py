"""
scheduler.py — APScheduler that sends one question per hour, 09:00–20:00 Prague time.
"""
from __future__ import annotations

import logging
import os
import html

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from database import count_questions, get_random_unsent, mark_as_sent

logger = logging.getLogger(__name__)

GROUP_ID: str = os.environ["GROUP_ID"]
ADMIN_ID: int | None = (
    int(os.getenv("ADMIN_ID", "0")) or None
)

TZ = "Europe/Prague"


def _format_message(q) -> str:
    """
    Build a Telegram HTML message with the answer hidden behind a spoiler.
    Pattern:
        📚 <b>Pack Name</b>  |  Вопрос N
        ─────────────────
        <question text>

        <tg-spoiler>💡 <b>Ответ:</b> answer text</tg-spoiler>
    """
    pack = html.escape(q.pack_name)
    text = html.escape(q.text)
    answer_part = (
        f"\n\n<tg-spoiler>💡 <b>Ответ:</b> {html.escape(q.answer)}</tg-spoiler>"
        if q.answer
        else ""
    )
    qnum = q.question_number or "?"
    return (
        f"📚 <b>{pack}</b>  |  Вопрос {qnum}\n"
        f"{'─' * 30}\n\n"
        f"{text}"
        f"{answer_part}"
    )


async def send_question(bot: Bot) -> None:
    """Pick a random unsent question and send it to the group."""
    q = await get_random_unsent()

    if q is None:
        remaining = await count_questions(unsent_only=True)
        logger.warning("No unsent questions left in DB (total unsent=%d).", remaining)
        if ADMIN_ID:
            await bot.send_message(
                ADMIN_ID,
                "⚠️ <b>Все вопросы отправлены!</b>\n"
                "Запусти /parse, чтобы загрузить новые.",
                parse_mode="HTML",
            )
        return

    msg = _format_message(q)
    if q.image_url:
        try:
            await bot.send_photo(
                GROUP_ID,
                photo=q.image_url,
                caption=msg,
                parse_mode="HTML",
            )
        except Exception:
            # Fallback: send text only if image fails
            await bot.send_message(GROUP_ID, msg, parse_mode="HTML")
    else:
        await bot.send_message(GROUP_ID, msg, parse_mode="HTML")
    await mark_as_sent(q.id)
    logger.info("Sent question id=%d ('%s') to group %s.", q.id, q.link, GROUP_ID)


def build_scheduler(bot: Bot) -> AsyncIOScheduler:
    """
    Create and configure the scheduler.
    Jobs fire every hour at :00 minutes, between 09:00 and 20:00 Prague time.
    """
    scheduler = AsyncIOScheduler(timezone=TZ)

    # Fire at 9,10,11,12,13,14,15,16,17,18,19,20 o'clock every day
    scheduler.add_job(
        send_question,
        trigger=CronTrigger(
            hour="9-20",
            minute="0",
            timezone=TZ,
        ),
        kwargs={"bot": bot},
        id="send_question",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=300,
    )

    return scheduler
