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

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import (
    PAGE_SIZE,
    clear_questions,
    count_questions,
    get_adjacent_question_ids,
    get_question_by_id,
    get_questions_paged,
    init_db,
    mark_as_sent,
)
from scheduler import ADMIN_ID, GROUP_ID, _format_message, build_scheduler, send_question
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

# ─────────────────────────────────────────────────────────────────────────────
# Keyboard builders
# ─────────────────────────────────────────────────────────────────────────────

def _questions_kb(
    questions: list,
    page: int,
    total: int,
    unsent_only: bool,
) -> InlineKeyboardMarkup:
    """Build inline keyboard for the questions list view."""
    builder = InlineKeyboardBuilder()
    filter_flag = "1" if unsent_only else "0"

    for q in questions:
        # Use first non-empty line as preview (clean after scraper fix)
        first_line = next((l.strip() for l in q.text.splitlines() if l.strip()), q.text)
        short = first_line[:52]
        if len(first_line) > 52:
            short += "…"
        sent_mark = "✅ " if q.is_sent else ""
        builder.row(
            InlineKeyboardButton(
                text=f"{sent_mark}В{q.question_number}: {short}",
                callback_data=f"q_view:{q.id}:{page}:{filter_flag}",
            )
        )

    # Pagination row
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"q_page:{page-1}:{filter_flag}"))

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    nav.append(
        InlineKeyboardButton(
            text=f"{page + 1}/{total_pages}",
            callback_data="noop",
        )
    )

    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"q_page:{page+1}:{filter_flag}"))

    builder.row(*nav)

    # Filter toggle
    toggle_label = "👁 Показать все" if unsent_only else "🔲 Только неотправленные"
    toggle_flag = "0" if unsent_only else "1"
    builder.row(
        InlineKeyboardButton(text=toggle_label, callback_data=f"q_page:0:{toggle_flag}")
    )

    return builder.as_markup()


def _question_detail_kb(
    q_id: int,
    page: int,
    filter_flag: str,
    prev_id: int | None = None,
    next_id: int | None = None,
) -> InlineKeyboardMarkup:
    """Keyboard for a single question: prev/next, send to group, back to list."""
    builder = InlineKeyboardBuilder()

    # Prev / Next navigation row
    nav: list[InlineKeyboardButton] = []
    if prev_id is not None:
        nav.append(InlineKeyboardButton(
            text="◀️ Пред.",
            callback_data=f"q_view:{prev_id}:{page}:{filter_flag}",
        ))
    if next_id is not None:
        nav.append(InlineKeyboardButton(
            text="След. ▶️",
            callback_data=f"q_view:{next_id}:{page}:{filter_flag}",
        ))
    if nav:
        builder.row(*nav)

    builder.row(
        InlineKeyboardButton(
            text="📤 Отправить в группу",
            callback_data=f"q_send:{q_id}:{page}:{filter_flag}",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="◀️ Назад к списку",
            callback_data=f"q_page:{page}:{filter_flag}",
        )
    )
    return builder.as_markup()


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
        "/questions — просмотр вопросов и отправка в группу\n"
        "/parse — запарсить Балканфест-2025 (только для админа)\n"
        "/reparse — очистить базу и запарсить заново (только для админа)\n"
        "/parse_all — запарсить ВСЕ 6 550 пакетов (только для админа)\n"
        "/send_now — отправить случайный вопрос прямо сейчас (только для админа)",
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


@router.message(Command("questions"))
async def cmd_questions(message: Message) -> None:
    """List questions with inline pagination and per-question send button."""
    page = 0
    unsent_only = True
    questions = await get_questions_paged(page=page, unsent_only=unsent_only)
    total = await count_questions(unsent_only=unsent_only)

    if not questions:
        await message.answer(
            "📭 Нет вопросов в базе.\nЗапусти /parse чтобы загрузить."
        )
        return

    pack_name = questions[0].pack_name if questions else ""
    kb = _questions_kb(questions, page, total, unsent_only)
    await message.answer(
        f"📚 <b>{pack_name}</b>\n"
        f"Вопросов (неотправленных: <b>{total}</b>)\n"
        "Нажми на вопрос чтобы посмотреть и отправить в группу.",
        parse_mode="HTML",
        reply_markup=kb,
    )


# ── callback: pagination ──────────────────────────────────────────────────────
@router.callback_query(lambda c: c.data and c.data.startswith("q_page:"))
async def cb_questions_page(callback: CallbackQuery) -> None:
    _, page_str, filter_flag = callback.data.split(":")
    page = int(page_str)
    unsent_only = filter_flag == "1"

    questions = await get_questions_paged(page=page, unsent_only=unsent_only)
    total = await count_questions(unsent_only=unsent_only)

    if not questions and page > 0:
        # Went past last page after sending — step back
        page = max(0, page - 1)
        questions = await get_questions_paged(page=page, unsent_only=unsent_only)

    pack_name = questions[0].pack_name if questions else ""
    kb = _questions_kb(questions, page, total, unsent_only)
    label = "неотправленных" if unsent_only else "всего"
    await callback.message.edit_text(
        f"📚 <b>{pack_name}</b>\n"
        f"Вопросов ({label}: <b>{total}</b>)\n"
        "Нажми на вопрос чтобы посмотреть и отправить в группу.",
        parse_mode="HTML",
        reply_markup=kb,
    )
    await callback.answer()


# ── callback: view single question ───────────────────────────────────────────
@router.callback_query(lambda c: c.data and c.data.startswith("q_view:"))
async def cb_question_view(callback: CallbackQuery) -> None:
    _, q_id_str, page_str, filter_flag = callback.data.split(":")
    q = await get_question_by_id(int(q_id_str))
    page = int(page_str)
    unsent_only = filter_flag == "1"

    if q is None:
        await callback.answer("Вопрос не найден.", show_alert=True)
        return

    prev_id, next_id = await get_adjacent_question_ids(q.id, unsent_only=unsent_only)

    import html as _html
    sent_status = "✅ уже отправлен" if q.is_sent else "🔲 не отправлен"
    text = (
        f"📚 <b>{_html.escape(q.pack_name)}</b>  |  Вопрос {q.question_number}\n"
        f"{'─' * 30}\n\n"
        f"{_html.escape(q.text)}"
    )
    if q.answer:
        text += f"\n\n<tg-spoiler>💡 <b>Ответ:</b> {_html.escape(q.answer)}</tg-spoiler>"
    text += f"\n\n<i>Статус: {sent_status}</i>"

    kb = _question_detail_kb(q.id, page, filter_flag, prev_id=prev_id, next_id=next_id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


# ── callback: send question to group ─────────────────────────────────────────
@router.callback_query(lambda c: c.data and c.data.startswith("q_send:"))
async def cb_send_to_group(callback: CallbackQuery, bot: Bot) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Только для администратора.", show_alert=True)
        return

    _, q_id_str, page_str, filter_flag = callback.data.split(":")
    q = await get_question_by_id(int(q_id_str))

    if q is None:
        await callback.answer("Вопрос не найден.", show_alert=True)
        return

    if q.is_sent:
        await callback.answer("Этот вопрос уже был отправлен.", show_alert=True)
        return

    msg = _format_message(q)
    await bot.send_message(GROUP_ID, msg, parse_mode="HTML")
    await mark_as_sent(q.id)
    logger.info("Manually sent question id=%d to group %s.", q.id, GROUP_ID)

    await callback.answer("✅ Отправлено в группу!", show_alert=False)

    # Refresh back to the list
    page = int(page_str)
    unsent_only = filter_flag == "1"
    questions = await get_questions_paged(page=page, unsent_only=unsent_only)
    total = await count_questions(unsent_only=unsent_only)
    if not questions and page > 0:
        page -= 1
        questions = await get_questions_paged(page=page, unsent_only=unsent_only)
    pack_name = questions[0].pack_name if questions else ""
    kb = _questions_kb(questions, page, total, unsent_only)
    label = "неотправленных" if unsent_only else "всего"
    await callback.message.edit_text(
        f"📚 <b>{pack_name}</b>\n"
        f"Вопросов ({label}: <b>{total}</b>)\n"
        "Нажми на вопрос чтобы посмотреть и отправить в группу.",
        parse_mode="HTML",
        reply_markup=kb,
    )


# ── callback: noop (page counter button) ─────────────────────────────────────
@router.callback_query(lambda c: c.data == "noop")
async def cb_noop(callback: CallbackQuery) -> None:
    await callback.answer()


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


@router.message(Command("reparse"))
async def cmd_reparse(message: Message) -> None:
    """Clear all questions and re-scrape the current pack from scratch."""
    if not _is_admin(message.from_user.id):
        await message.answer("⛔ Только для администратора.")
        return

    deleted = await clear_questions()
    await message.answer(
        f"🗑 Удалено вопросов из базы: <b>{deleted}</b>\n"
        f"🔄 Запускаю парсинг пакета {FIRST_PACK_ID} заново…",
        parse_mode="HTML",
    )
    try:
        inserted = await scrape_first_pack()
        await message.answer(
            f"✅ Готово! Добавлено вопросов: <b>{inserted}</b>",
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.exception("Reparse failed")
        await message.answer(f"❌ Ошибка парсинга:\n<code>{exc}</code>", parse_mode="HTML")


# ─────────────────────────────────────────────────────────────────────────────
# Background helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _background_parse(bot: Bot) -> None:
    """Parse Балканфест-2025 in the background; notify admin when done."""
    inserted = 0
    parse_ok = False
    try:
        inserted = await scrape_first_pack()
        logger.info("Background auto-parse complete: %d questions inserted.", inserted)
        parse_ok = True
    except Exception:
        logger.exception("Background auto-parse failed.")

    # Notify admin — isolated so a failed DM never masks parse status
    if ADMIN_ID:
        try:
            if parse_ok:
                await bot.send_message(
                    ADMIN_ID,
                    f"✅ <b>Авто-парсинг завершён!</b>\nДобавлено вопросов: <b>{inserted}</b>",
                    parse_mode="HTML",
                )
            else:
                await bot.send_message(
                    ADMIN_ID,
                    "❌ <b>Авто-парсинг завершился с ошибкой.</b>\nПопробуй /parse вручную.",
                    parse_mode="HTML",
                )
        except Exception as notify_err:
            logger.warning("Could not notify admin after parse: %s", notify_err)


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

    # ── Startup hook: fires after polling connects, inside the running loop ──
    _scheduler = None

    @dp.startup()
    async def on_startup() -> None:
        nonlocal _scheduler
        _scheduler = build_scheduler(bot)
        _scheduler.start()
        logger.info("Scheduler started. Will send questions 09–20 (Europe/Prague).")

        if await count_questions() == 0:
            logger.info("DB is empty — launching background auto-parse of Балканфест-2025…")
            asyncio.get_event_loop().create_task(_background_parse(bot))

    # ── Shutdown hook ────────────────────────────────────────────────────────
    @dp.shutdown()
    async def on_shutdown() -> None:
        if _scheduler:
            _scheduler.shutdown(wait=False)

    try:
        await dp.start_polling(
            bot,
            allowed_updates=["message", "callback_query"],
        )
    except Exception:
        logger.exception("Polling crashed!")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
