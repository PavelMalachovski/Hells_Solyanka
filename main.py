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
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
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
    count_questions_by_pack,
    get_adjacent_in_pack,
    get_adjacent_question_ids,
    get_packs,
    get_question_by_id,
    get_questions_by_pack,
    get_questions_paged,
    init_db,
    mark_as_sent,
)
from scheduler import ADMIN_ID, GROUP_ID, _build_group_text, _group_question_kb, build_scheduler, send_question
from scraper import BASE_URL, FIRST_PACK_ID, scrape_all_packs, scrape_first_pack, scrape_pack

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
# Keyboard builders
# ─────────────────────────────────────────────────────────────────────────────

def _packs_kb(packs: list[dict], unsent_only: bool) -> InlineKeyboardMarkup:
    """Inline keyboard showing one button per pack."""
    builder = InlineKeyboardBuilder()
    filter_flag = "1" if unsent_only else "0"
    for p in packs:
        cnt = p["unsent"] if unsent_only else p["total"]
        builder.row(InlineKeyboardButton(
            text=f"📚 {p['pack_name']}  ({cnt})",
            callback_data=f"pq_page:{p['pack_id']}:0:{filter_flag}",
        ))
    toggle_label = "👁 Показать все" if unsent_only else "🔲 Только неотправленные"
    builder.row(InlineKeyboardButton(
        text=toggle_label,
        callback_data=f"pk_list:{('0' if unsent_only else '1')}",
    ))
    return builder.as_markup()


def _pack_questions_kb(
    questions: list,
    pack_id: str,
    page: int,
    total: int,
    unsent_only: bool,
) -> InlineKeyboardMarkup:
    """Inline keyboard listing questions inside a pack."""
    builder = InlineKeyboardBuilder()
    filter_flag = "1" if unsent_only else "0"
    for q in questions:
        first_line = next((l.strip() for l in q.text.splitlines() if l.strip()), q.text)
        short = first_line[:50] + ("…" if len(first_line) > 50 else "")
        sent_mark = "✅ " if q.is_sent else ""
        builder.row(InlineKeyboardButton(
            text=f"{sent_mark}В{q.question_number}: {short}",
            callback_data=f"q_view:{q.id}:{pack_id}:{page}:{filter_flag}",
        ))
    # Pagination
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"pq_page:{pack_id}:{page-1}:{filter_flag}"))
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="noop"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"pq_page:{pack_id}:{page+1}:{filter_flag}"))
    builder.row(*nav)
    # Toggle + back
    toggle_label = "👁 Показать все" if unsent_only else "🔲 Только неотправленные"
    toggle_flag = "0" if unsent_only else "1"
    builder.row(InlineKeyboardButton(text=toggle_label, callback_data=f"pq_page:{pack_id}:0:{toggle_flag}"))
    builder.row(InlineKeyboardButton(text="◀️ К списку пакетов", callback_data=f"pk_list:{filter_flag}"))
    return builder.as_markup()


def _question_detail_kb(
    q_id: int,
    pack_id: str,
    page: int,
    filter_flag: str,
    prev_id: int | None = None,
    next_id: int | None = None,
    has_answer: bool = False,
    answer_shown: bool = False,
    has_source: bool = False,
    source_shown: bool = False,
) -> InlineKeyboardMarkup:
    """Keyboard for a single question: prev/next, show/hide answer, show source, send, back."""
    builder = InlineKeyboardBuilder()
    nav: list[InlineKeyboardButton] = []
    if prev_id is not None:
        nav.append(InlineKeyboardButton(
            text="◀️ Пред.",
            callback_data=f"q_view:{prev_id}:{pack_id}:{page}:{filter_flag}",
        ))
    if next_id is not None:
        nav.append(InlineKeyboardButton(
            text="След. ▶️",
            callback_data=f"q_view:{next_id}:{pack_id}:{page}:{filter_flag}",
        ))
    if nav:
        builder.row(*nav)
    if has_answer and not answer_shown:
        builder.row(InlineKeyboardButton(
            text="💡 Показать ответ",
            callback_data=f"q_ans:{q_id}:{pack_id}:{page}:{filter_flag}",
        ))
    if answer_shown:
        builder.row(InlineKeyboardButton(
            text="🙈 Скрыть ответ",
            callback_data=f"q_view:{q_id}:{pack_id}:{page}:{filter_flag}",
        ))
    if answer_shown and has_source and not source_shown:
        builder.row(InlineKeyboardButton(
            text="📎 Показать источник",
            callback_data=f"q_src:{q_id}:{pack_id}:{page}:{filter_flag}",
        ))
    builder.row(InlineKeyboardButton(
        text="📤 Отправить в группу",
        callback_data=f"q_send:{q_id}:{pack_id}:{page}:{filter_flag}",
    ))
    builder.row(InlineKeyboardButton(
        text="◀️ Назад к вопросам",
        callback_data=f"pq_page:{pack_id}:{page}:{filter_flag}",
    ))
    return builder.as_markup()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

import html as _html_mod


def _build_question_text(q) -> str:
    """Build HTML message text for a question view (without answer)."""
    text = (
        f"📚 <b>{_html_mod.escape(q.pack_name)}</b>  |  Вопрос {q.question_number}\n"
        f"{'─' * 30}\n\n"
        f"{_html_mod.escape(q.text)}"
    )
    if q.image_url:
        text += f'\n\n🖼 <a href="{q.image_url}">Раздаточный материал</a>'
    return text


def _format_source_html(source: str) -> str:
    """Wrap bare URLs in <a href> for clickability; escape plain text lines."""
    import re as _re
    lines = []
    for line in source.splitlines():
        line = line.strip()
        if not line:
            continue
        # Numbered prefix like "1. https://..."
        m = _re.match(r'^(\d+\.\s*)(https?://\S+)$', line)
        if m:
            lines.append(f'{m.group(1)}<a href="{m.group(2)}">{m.group(2)}</a>')
        elif _re.match(r'^https?://', line):
            lines.append(f'<a href="{line}">{line}</a>')
        else:
            lines.append(_html_mod.escape(line))
    return "\n".join(lines)


def _build_question_text_with_answer(q, include_source: bool = False) -> str:
    """Build HTML message text for a question view with answer (optionally with source)."""
    text = _build_question_text(q)
    text += f"\n\n💡 <b>Ответ:</b> {_html_mod.escape(q.answer or '')}"
    if include_source and q.source:
        text += f"\n\n📎 <b>Источник:</b>\n{_format_source_html(q.source)}"
    return text


# ─────────────────────────────────────────────────────────────────────────────
# Handlers
# ─────────────────────────────────────────────────────────────────────────────


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


async def _send_with_image(
    bot: Bot,
    chat_id: str | int,
    msg: str,
    image_url: str | None,
    reply_markup=None,
) -> None:
    """Send message to chat, attaching image as uploaded bytes if available."""
    if image_url:
        data = await asyncio.to_thread(_download_image_sync, image_url)
        if data:
            try:
                caption = msg if len(msg) <= 1024 else msg[:1021] + "…"
                await bot.send_photo(
                    chat_id,
                    photo=BufferedInputFile(data, filename="image.jpg"),
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                )
                if len(msg) > 1024:
                    await bot.send_message(chat_id, msg, parse_mode="HTML")
                return
            except Exception as exc:
                logger.warning("send_photo failed: %s", exc)
    await bot.send_message(chat_id, msg, parse_mode="HTML", reply_markup=reply_markup)


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
        "/questions — просмотр пакетов и отправка в группу\n"
        "/parse — запарсить Балканфест-2025 (админ)\n"
        "/parse_pack НОМЕР — запарсить пакет по номеру (админ)\n"
        "/reparse — очистить базу и парсить заново (админ)\n"
        "/parse_all — запарсить ВСЕ 6 550 пакетов (админ)\n"
        "/send_now — отправить случайный вопрос сейчас (админ)",
        parse_mode="HTML",
    )


@router.message(Command("questions"))
async def cmd_questions(message: Message) -> None:
    unsent_only = True
    packs = await get_packs(unsent_only=unsent_only)
    kb = _packs_kb(packs, unsent_only=unsent_only)
    total_unsent = sum(p["unsent"] for p in packs)
    await message.answer(
        f"📖 <b>Пакеты</b> (неотправленных всего: <b>{total_unsent}</b>)\n"
        "Нажми на пакет чтобы выбрать вопрос.",
        parse_mode="HTML", reply_markup=kb,
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


# ── callback: view single question ───────────────────────────────────────────
@router.callback_query(lambda c: c.data and c.data.startswith("q_view:"))
async def cb_question_view(callback: CallbackQuery) -> None:
    # q_view:{q_id}:{pack_id}:{page}:{filter}
    parts = callback.data.split(":")
    q_id, pack_id, page_str, filter_flag = parts[1], parts[2], parts[3], parts[4]
    q = await get_question_by_id(int(q_id))
    page = int(page_str)
    unsent_only = filter_flag == "1"

    if q is None:
        await callback.answer("Вопрос не найден.", show_alert=True)
        return

    pack_link = f"{BASE_URL}/pack/{pack_id}"
    prev_id, next_id = await get_adjacent_in_pack(q.id, pack_link, unsent_only=unsent_only)

    text = _build_question_text(q)
    kb = _question_detail_kb(
        q.id, pack_id, page, filter_flag,
        prev_id=prev_id, next_id=next_id,
        has_answer=bool(q.answer),
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


# ── callback: show answer for 10 seconds then auto-hide ───────────────────────
@router.callback_query(lambda c: c.data and c.data.startswith("q_ans:"))
async def cb_show_answer(callback: CallbackQuery) -> None:
    # q_ans:{q_id}:{pack_id}:{page}:{filter}
    parts = callback.data.split(":")
    q_id, pack_id, page_str, filter_flag = parts[1], parts[2], parts[3], parts[4]
    q = await get_question_by_id(int(q_id))
    page = int(page_str)
    unsent_only = filter_flag == "1"

    if q is None:
        await callback.answer("Вопрос не найден.", show_alert=True)
        return

    pack_link = f"{BASE_URL}/pack/{pack_id}"
    prev_id, next_id = await get_adjacent_in_pack(q.id, pack_link, unsent_only=unsent_only)

    base_text = _build_question_text(q)
    text_with_answer = _build_question_text_with_answer(q)

    kb = _question_detail_kb(
        q.id, pack_id, page, filter_flag,
        prev_id=prev_id, next_id=next_id,
        has_answer=True,
        answer_shown=True,
        has_source=bool(q.source),
        source_shown=False,
    )

    try:
        await callback.message.edit_text(text_with_answer, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest:
        pass  # Already showing the answer — ignore double-tap
    await callback.answer()


# ── callback: show source ────────────────────────────────────────────────────────────────
@router.callback_query(lambda c: c.data and c.data.startswith("q_src:"))
async def cb_show_source(callback: CallbackQuery) -> None:
    # q_src:{q_id}:{pack_id}:{page}:{filter}
    parts = callback.data.split(":")
    q_id, pack_id, page_str, filter_flag = parts[1], parts[2], parts[3], parts[4]
    q = await get_question_by_id(int(q_id))
    page = int(page_str)
    unsent_only = filter_flag == "1"

    if q is None or not q.source:
        await callback.answer("Источник не найден.", show_alert=True)
        return

    pack_link = f"{BASE_URL}/pack/{pack_id}"
    prev_id, next_id = await get_adjacent_in_pack(q.id, pack_link, unsent_only=unsent_only)

    text = _build_question_text_with_answer(q, include_source=True)
    kb = _question_detail_kb(
        q.id, pack_id, page, filter_flag,
        prev_id=prev_id, next_id=next_id,
        has_answer=True,
        answer_shown=True,
        has_source=True,
        source_shown=True,
    )
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest:
        pass
    await callback.answer()

# ── group callbacks: answer and source as private popups (only visible to the tapper) ───
@router.callback_query(lambda c: c.data and c.data.startswith("gq_ans:"))
async def cb_group_show_answer(callback: CallbackQuery) -> None:
    q_id = int(callback.data.split(":")[1])
    q = await get_question_by_id(q_id)
    if q is None or not q.answer:
        await callback.answer("Вопрос не найден.", show_alert=True)
        return
    await callback.answer(f"💡 Ответ: {q.answer}", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("gq_src:"))
async def cb_group_show_source(callback: CallbackQuery) -> None:
    q_id = int(callback.data.split(":")[1])
    q = await get_question_by_id(q_id)
    if q is None or not q.source:
        await callback.answer("Источник не найден.", show_alert=True)
        return
    await callback.answer(f"📎 Источник:\n{q.source}", show_alert=True)

# ── callback: send question to group ─────────────────────────────────────────
@router.callback_query(lambda c: c.data and c.data.startswith("q_send:"))
async def cb_send_to_group(callback: CallbackQuery, bot: Bot) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Только для администратора.", show_alert=True)
        return

    # q_send:{q_id}:{pack_id}:{page}:{filter}
    parts = callback.data.split(":")
    q_id, pack_id, page_str, filter_flag = parts[1], parts[2], parts[3], parts[4]
    q = await get_question_by_id(int(q_id))

    if q is None:
        await callback.answer("Вопрос не найден.", show_alert=True)
        return
    if q.is_sent:
        await callback.answer("Этот вопрос уже был отправлен.", show_alert=True)
        return

    msg = _build_group_text(q)
    kb = _group_question_kb(q.id, has_answer=bool(q.answer), has_source=bool(q.source))
    await _send_with_image(bot, GROUP_ID, msg, q.image_url, reply_markup=kb)
    await mark_as_sent(q.id)
    logger.info("Manually sent question id=%d to group.", q.id)
    await callback.answer("✅ Отправлено в группу!", show_alert=False)

    # Refresh back to the pack question list
    page = int(page_str)
    unsent_only = filter_flag == "1"
    pack_link = f"{BASE_URL}/pack/{pack_id}"
    questions = await get_questions_by_pack(pack_link, page=page, unsent_only=unsent_only)
    total = await count_questions_by_pack(pack_link, unsent_only=unsent_only)
    if not questions and page > 0:
        page -= 1
        questions = await get_questions_by_pack(pack_link, page=page, unsent_only=unsent_only)
    pack_name = questions[0].pack_name if questions else pack_id
    label = "неотправленных" if unsent_only else "всего"
    kb = _pack_questions_kb(questions, pack_id, page, total, unsent_only)
    await callback.message.edit_text(
        f"📚 <b>{pack_name}</b>\n"
        f"Вопросов ({label}: <b>{total}</b>)\n"
        "Нажми на вопрос чтобы посмотреть и отправить.",
        parse_mode="HTML", reply_markup=kb,
    )


# ── callback: pack list (with filter toggle or back from pack questions) ─────
@router.callback_query(lambda c: c.data and c.data.startswith("pk_list:"))
async def cb_pack_list(callback: CallbackQuery) -> None:
    # pk_list:{filter_flag}
    filter_flag = callback.data.split(":")[1]
    unsent_only = filter_flag == "1"
    packs = await get_packs(unsent_only=unsent_only)
    kb = _packs_kb(packs, unsent_only=unsent_only)
    total_unsent = sum(p["unsent"] for p in packs)
    await callback.message.edit_text(
        f"📖 <b>Пакеты</b> (неотправленных всего: <b>{total_unsent}</b>)\n"
        "Нажми на пакет чтобы выбрать вопрос.",
        parse_mode="HTML", reply_markup=kb,
    )
    await callback.answer()


# ── callback: questions list inside a pack ────────────────────────────────────
@router.callback_query(lambda c: c.data and c.data.startswith("pq_page:"))
async def cb_pack_questions(callback: CallbackQuery) -> None:
    # pq_page:{pack_id}:{page}:{filter_flag}
    parts = callback.data.split(":")
    pack_id, page_str, filter_flag = parts[1], parts[2], parts[3]
    page = int(page_str)
    unsent_only = filter_flag == "1"
    pack_link = f"{BASE_URL}/pack/{pack_id}"
    questions = await get_questions_by_pack(pack_link, page=page, unsent_only=unsent_only)
    total = await count_questions_by_pack(pack_link, unsent_only=unsent_only)
    if not questions and page > 0:
        page -= 1
        questions = await get_questions_by_pack(pack_link, page=page, unsent_only=unsent_only)
        total = await count_questions_by_pack(pack_link, unsent_only=unsent_only)
    pack_name = questions[0].pack_name if questions else f"Пакет {pack_id}"
    label = "неотправленных" if unsent_only else "всего"
    kb = _pack_questions_kb(questions, pack_id, page, total, unsent_only)
    await callback.message.edit_text(
        f"📚 <b>{pack_name}</b>\n"
        f"Вопросов ({label}): <b>{total}</b>\n"
        "Нажми на вопрос чтобы посмотреть и отправить.",
        parse_mode="HTML", reply_markup=kb,
    )
    await callback.answer()


# ── fallback: old-style q_page callbacks (stale buttons from previous version)
@router.callback_query(lambda c: c.data and c.data.startswith("q_page:"))
async def cb_old_qpage(callback: CallbackQuery) -> None:
    packs = await get_packs(unsent_only=True)
    kb = _packs_kb(packs, unsent_only=True)
    total_unsent = sum(p["unsent"] for p in packs)
    await callback.message.edit_text(
        f"📖 <b>Пакеты</b> (неотправленных всего: <b>{total_unsent}</b>)\n"
        "Нажми на пакет чтобы выбрать вопрос.",
        parse_mode="HTML", reply_markup=kb,
    )
    await callback.answer()


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


@router.message(Command("parse_pack"))
async def cmd_parse_pack(message: Message) -> None:
    """Parse one pack by its numeric ID: /parse_pack 1234"""
    if not _is_admin(message.from_user.id):
        await message.answer("⛔ Только для администратора.")
        return

    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer(
            "❌ Укажи номер пакета: <code>/parse_pack 1234</code>",
            parse_mode="HTML",
        )
        return

    pack_id = int(parts[1])
    pack_url = f"{BASE_URL}/pack/{pack_id}"

    from playwright.async_api import async_playwright
    await message.answer(f"🔄 Запускаю парсинг pack/{pack_id}…")
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                inserted = await scrape_pack(pack_url, browser)
            finally:
                await browser.close()
        await message.answer(
            f"✅ Готово! Добавлено вопросов: <b>{inserted}</b>",
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.exception("parse_pack %d failed", pack_id)
        await message.answer(f"❌ Ошибка парсинга:\n<code>{exc}</code>", parse_mode="HTML")


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
        logger.info("Scheduler started. Will send question at 10:00 (Europe/Prague).")

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
