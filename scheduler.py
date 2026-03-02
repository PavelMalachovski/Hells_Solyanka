"""
scheduler.py — APScheduler that sends one question per hour, 09:00–20:00 Prague time.
"""
from __future__ import annotations

import asyncio
import logging
import os
import html
import re

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
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
    """Legacy: plain text for backward compat. Use _build_group_text for new sends."""
    pack = html.escape(q.pack_name)
    text = html.escape(q.text)
    qnum = q.question_number or "?"
    result = (
        f"📚 <b>{pack}</b>  |  Вопрос {qnum}\n"
        f"{'─' * 30}\n\n"
        f"{text}"
    )
    if q.image_url:
        result += f'\n\n🖼 <a href="{q.image_url}">Раздаточный материал</a>'
    return result


def _format_source_html_group(source: str) -> str:
    """Wrap bare URLs in <a href> for clickability; escape plain text lines."""
    lines = []
    for line in source.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r'^(\d+\.\s*)(https?://\S+)$', line)
        if m:
            lines.append(f'{m.group(1)}<a href="{m.group(2)}">{m.group(2)}</a>')
        elif re.match(r'^https?://', line):
            lines.append(f'<a href="{line}">{line}</a>')
        else:
            lines.append(html.escape(line))
    return "\n".join(lines)


def _build_group_text(
    q,
    answer_shown: bool = False,
    source_shown: bool = False,
) -> str:
    """Build group message text (question only, optionally with answer/source)."""
    pack = html.escape(q.pack_name)
    text = html.escape(q.text)
    qnum = q.question_number or "?"
    result = (
        f"📚 <b>{pack}</b>  |  Вопрос {qnum}\n"
        f"{'─' * 30}\n\n"
        f"{text}"
    )
    if q.image_url:
        result += f'\n\n🖼 <a href="{q.image_url}">Раздаточный материал</a>'
    if answer_shown and q.answer:
        result += f"\n\n💡 <b>Ответ:</b> {html.escape(q.answer)}"
    if source_shown and q.source:
        result += f"\n\n📎 <b>Источник:</b>\n{_format_source_html_group(q.source)}"
    return result


def _group_question_kb(
    q_id: int,
    has_answer: bool = False,
    has_source: bool = False,
) -> InlineKeyboardMarkup:
    """Keyboard for group messages: show answer and source as private popups."""
    builder = InlineKeyboardBuilder()
    if has_answer:
        builder.row(InlineKeyboardButton(
            text="💡 Показать ответ",
            callback_data=f"gq_ans:{q_id}",
        ))
    if has_source:
        builder.row(InlineKeyboardButton(
            text="📎 Показать источник",
            callback_data=f"gq_src:{q_id}",
        ))
    return builder.as_markup()


def _download_image_sync(url: str) -> bytes | None:
    """Blocking download of image bytes (run in thread)."""
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read()
    except Exception as exc:
        logger.warning("Image download failed %s: %s", url, exc)
        return None


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

    msg = _build_group_text(q)
    kb = _group_question_kb(q.id, has_answer=bool(q.answer), has_source=bool(q.source))
    if q.image_url:
        data = await asyncio.to_thread(_download_image_sync, q.image_url)
        if data:
            try:
                from aiogram.types import BufferedInputFile
                caption = msg if len(msg) <= 1024 else msg[:1021] + "…"
                await bot.send_photo(
                    GROUP_ID,
                    photo=BufferedInputFile(data, filename="image.jpg"),
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=kb,
                )
                if len(msg) > 1024:
                    await bot.send_message(GROUP_ID, msg, parse_mode="HTML")
                await mark_as_sent(q.id)
                logger.info("Sent question id=%d with photo.", q.id)
                return
            except Exception as exc:
                logger.warning("send_photo failed, falling back to text: %s", exc)
    await bot.send_message(GROUP_ID, msg, parse_mode="HTML", reply_markup=kb)
    await mark_as_sent(q.id)
    logger.info("Sent question id=%d.", q.id)


async def send_morning_questions(bot: Bot) -> None:
    """Send a morning greeting and 4 questions to the group at 10:00."""
    await bot.send_message(GROUP_ID, "Доброе утро, Аццкая Солянка 🔥", parse_mode="HTML")
    for _ in range(4):
        await send_question(bot)


async def send_evening_questions(bot: Bot) -> None:
    """Send an evening greeting and 4 questions to the group at 20:00."""
    await bot.send_message(GROUP_ID, "Добрый вечер, Аццкая Солянка 🔥", parse_mode="HTML")
    for _ in range(4):
        await send_question(bot)


def build_scheduler(bot: Bot) -> AsyncIOScheduler:
    """
    Create and configure the scheduler.
    Sends 4 questions with a greeting at 10:00 and 20:00 Prague time.
    """
    scheduler = AsyncIOScheduler(timezone=TZ)

    # Morning block: greeting + 4 questions at 10:00
    scheduler.add_job(
        send_morning_questions,
        trigger=CronTrigger(hour="10", minute="0", timezone=TZ),
        kwargs={"bot": bot},
        id="send_morning_questions",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=300,
    )

    # Evening block: greeting + 4 questions at 20:00
    scheduler.add_job(
        send_evening_questions,
        trigger=CronTrigger(hour="20", minute="0", timezone=TZ),
        kwargs={"bot": bot},
        id="send_evening_questions",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=300,
    )

    return scheduler
