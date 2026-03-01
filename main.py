"""
main.py — Entry point. Sets up aiogram bot, registers handlers, starts scheduler.

IMPORTANT: load_dotenv() MUST run before importing any project module that reads
os.environ at module level (database.py, scheduler.py, scraper.py).
"""
from __future__ import annotations

# ── load .env FIRST (before project imports) ──────────────────────────────
from dotenv import load_dotenv
load_dotenv()
# ────────────────────────────────────────────────────────────────────────────

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import Message

from database import count_questions, init_db
from scheduler import ADMIN_ID, GROUP_ID, build_scheduler, send_question
from scraper import FIRST_PACK_ID, scrape_all_packs, scrape_first_pack

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

BOT_TOKEN: str = os.environ["BOT_TOKEN"]

router = Router()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_admin(user_id: int) -> bool:
    return ADMIN_ID is None or user_id == ADMIN_ID


# ─────────────────────────────────────────────────────────────────────────────
# Handlers
# ─────────────────────────────────────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    total = await count_questions()
    unsent = await count_questions(unsent_only=True)
    await message.answer(
        "👋 <b>Hells Solyanka Bot</b>\n\n"
        f"Вопросов в базе: <b>{total}</b>\n"
        f"Ещё не отправлено: <b>{unsent}</b>\n\n"
        "Команды:\n"
        "/status — статистика базы\n"
        "/parse — запарсить Балканфест-2025 (только для админа)\n"
        "/parse_all — запарсить ВСЕ 6 550 пакетов (только для админа)\n"
        "/send_now — отправить вопрос прямо сейчас (только для админа)",
        parse_mode="HTML",
    )


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    total = await count_questions()
    unsent = await count_questions(unsent_only=True)
    sent = total - unsent
    await message.answer(
        "📊 <b>Статус базы</b>\n\n"
        f"Всего вопросов: <b>{total}</b>\n"
        f"Отправлено: <b>{sent}</b>\n"
        f"Ожидают отправки: <b>{unsent}</b>",
        parse_mode="HTML",
    )


@router.message(Command("parse"))
async def cmd_parse(message: Message, bot: Bot) -> None:
    if not _is_admin(message.from_user.id):
        await message.answer("⛔ Только для администратора.")
        return

    await message.answer(
        f"🔄 Запускаю парсинг пакета Балканфест-2025 (pack/{FIRST_PACK_ID})…\n"
        "Это займёт несколько минут.",
    )
    try:
        inserted = await scrape_first_pack()
        await message.answer(
            f"✅ Готово! Добавлено новых вопросов: <b>{inserted}</b>",
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.exception("Scraping failed")
        await message.answer(f"❌ Ошибка парсинга:\n<code>{exc}</code>", parse_mode="HTML")


@router.message(Command("parse_all"))
async def cmd_parse_all(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        await message.answer("⛔ Только для администратора.")
        return

    await message.answer(
        "🔄 Запускаю парсинг ВСЕХ пакетов (pack/1 → pack/{FIRST_PACK_ID}).\n"
        "⚠️ Это очень долго (часы). Прогресс в логах.",
    )
    try:
        total = await scrape_all_packs(start_id=1, end_id=FIRST_PACK_ID)
        await message.answer(
            f"✅ Полный парсинг завершён! Добавлено вопросов: <b>{total}</b>",
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.exception("Full scraping failed")
        await message.answer(f"❌ Ошибка: <code>{exc}</code>", parse_mode="HTML")


@router.message(Command("send_now"))
async def cmd_send_now(message: Message, bot: Bot) -> None:
    if not _is_admin(message.from_user.id):
        await message.answer("⛔ Только для администратора.")
        return
    await send_question(bot)
    await message.answer("✅ Вопрос отправлен в группу.")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    await init_db()

    total = await count_questions()
    logger.info("DB ready. Total questions: %d", total)

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    # Auto-parse Балканфест on first launch if DB is empty
    if total == 0:
        logger.info("DB is empty — auto-parsing Балканфест-2025 on startup…")
        try:
            inserted = await scrape_first_pack()
            logger.info("Auto-parse complete: %d questions inserted.", inserted)
        except Exception:
            logger.exception("Auto-parse failed on startup.")

    scheduler = build_scheduler(bot)
    scheduler.start()
    logger.info("Scheduler started. Will send questions 09–20 (Europe/Prague).")

    try:
        await dp.start_polling(bot, allowed_updates=["message"])
    finally:
        scheduler.shutdown()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
