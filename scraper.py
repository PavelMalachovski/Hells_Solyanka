"""
scraper.py — Playwright-based scraper for gotquestions.online.

Priority flow:
  1. Navigate to the pack page (default: Балканфест-2025 = pack/6705).
  2. Click the global "show answers" toggle (visibility_off icon).
  3. Walk through each question block and extract question text + answer.
  4. UPSERT into the DB.

For other packs: set PACK_ID env-var to the desired numeric ID, or
call `scrape_pack()` with any pack URL.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re

from playwright.async_api import Browser, Page, async_playwright

from database import upsert_questions

logger = logging.getLogger(__name__)

BASE_URL = "https://gotquestions.online"

# First pack to scrape — Балканфест-2025
FIRST_PACK_ID: int = int(os.getenv("PACK_ID", "6705"))


# ─────────────────────────────────────────────────────────────────────────────
async def _reveal_answers(page: Page) -> None:
    """Click the global eye-toggle (visibility_off) to reveal all answers."""
    try:
        # The icon is a Material-Icons <span> or <button> at the top of the pack
        toggle = page.locator("text=visibility_off").first
        if await toggle.count() > 0:
            await toggle.click()
            await page.wait_for_timeout(800)
            logger.debug("Clicked visibility_off toggle (show all answers).")
            return
    except Exception:
        pass

    # Fallback: click every single "Показать ответ" button
    buttons = await page.locator("text=Показать ответ").all()
    logger.debug("Clicking %d individual 'Показать ответ' buttons…", len(buttons))
    for btn in buttons:
        try:
            await btn.click()
            await page.wait_for_timeout(100)
        except Exception:
            pass


async def _parse_pack_page(page: Page, pack_url: str) -> list[dict]:
    """
    Extract all questions (and their answers) from an already-loaded pack page.

    Strategy:
      1. Collect all question URLs from the pack page.
      2. For each question URL, open the individual page, click "Показать ответ",
         and extract text + answer.

    Returns a list of dicts ready for upsert_questions().
    """
    # ---- pack title ----
    title_el = page.locator("h1, h2").first
    pack_name: str = (
        (await title_el.inner_text()).strip()
        if await title_el.count()
        else "Unknown Pack"
    )

    # Collect unique question hrefs from the pack page
    question_links = await page.locator("a[href*='/question/']").all()
    seen_hrefs: set[str] = set()
    q_urls: list[str] = []
    for link in question_links:
        href = await link.get_attribute("href") or ""
        if href and href not in seen_hrefs:
            seen_hrefs.add(href)
            full_url = BASE_URL + href if href.startswith("/") else href
            q_urls.append(full_url)

    logger.info("Found %d question URLs in pack '%s'", len(q_urls), pack_name)

    results: list[dict] = []
    browser = page.context.browser
    assert browser, "Browser must be available"

    for q_url in q_urls:
        try:
            q_data = await _fetch_single_question(browser, pack_name, pack_url, q_url)
            if q_data:
                results.append(q_data)
        except Exception as exc:
            logger.warning("Failed to scrape %s: %s", q_url, exc)
        # Small polite delay between individual question fetches
        await asyncio.sleep(0.5)

    return results


async def _fetch_single_question(
    browser: Browser,
    pack_name: str,
    pack_url: str,
    question_url: str,
) -> dict | None:
    """
    Open one question page, reveal the answer, and return a dict with all fields.
    """
    q_page = await browser.new_page()
    try:
        await q_page.goto(question_url, wait_until="networkidle", timeout=30_000)

        doc_q_number: int = 0
        # The question number shown on page (e.g. "Вопрос 42")
        num_el = q_page.locator("text=/Вопрос\\s+\\d+/").first
        if await num_el.count():
            nt = (await num_el.inner_text()).strip()
            m = re.search(r"\d+", nt)
            if m:
                doc_q_number = int(m.group())

        # Click "Показать ответ" to reveal the answer
        show_btn = q_page.locator("text=Показать ответ").first
        if await show_btn.count():
            await show_btn.click()
            await q_page.wait_for_timeout(600)

        # Get the full page text and parse
        body_text = await q_page.locator("body").inner_text()
        q_text, answer_text = _split_question_answer(body_text, "")

        # Cleanup: remove navigation / footer noise
        q_text = _clean_text(q_text)
        answer_text = _clean_text(answer_text)

        if not q_text:
            logger.warning("No question text found at %s", question_url)
            return None

        return {
            "pack_name": pack_name,
            "pack_link": pack_url,
            "question_number": doc_q_number,
            "text": q_text,
            "answer": answer_text or None,
            "link": question_url,
            "is_sent": False,
        }
    finally:
        await q_page.close()


def _clean_text(text: str) -> str:
    """Remove UI noise (icon names, buttons, footer) from extracted text."""
    noise = [
        "Поиск", "Пакеты", "Случайный пакет", "Люди", "Таймер", "О сайте",
        "thumb_up", "thumb_down", "bookmark_border", "bookmark", "share",
        "Показать ответ", "Скрыть ответ", "expand_more", "expand_less",
        "visibility_off", "visibility",
        "Есть вопросы? у нас есть",
        "О сайте", "Обратная связь", "Политика конфиденциальности", "Лицензирование",
        "Хотите печенья?", "ОтклонитьПринять",
    ]
    for n in noise:
        text = text.replace(n, " ")
    # Collapse multiple whitespace/newlines
    text = re.sub(r"\s{3,}", "\n\n", text).strip()
    # Trim lines
    lines = [l.strip() for l in text.splitlines()]
    return "\n".join(l for l in lines if l)[:3000]  # hard cap


def _split_question_answer(block: str, link_text: str) -> tuple[str, str]:
    """
    Given the full text block of a question card, extract:
      - question body (after the Вопрос N header, before the answer)
      - answer text (after the answer reveal)
    """
    # Remove the "Вопрос N" link prefix
    cleaned = block.replace(link_text, "", 1).strip()

    # Common patterns for answer separator on gotquestions.online:
    separators = [
        "Ответ:",
        "ОТВЕТ:",
        "Показать ответexpand_more",
        "Показать ответ",
        "Скрыть ответ",
        "expand_more",
    ]
    for sep in separators:
        if sep in cleaned:
            parts = cleaned.split(sep, 1)
            q_text = parts[0].strip()
            # Remove trailing UI noise
            q_text = re.sub(
                r"(thumb_up|thumb_down|bookmark_border|bookmark|share)\s*\d*",
                "",
                q_text,
            ).strip()
            answer = parts[1].strip() if len(parts) > 1 else ""
            # Clean up answer as well
            answer = re.sub(
                r"(thumb_up|thumb_down|bookmark_border|bookmark|share)\s*\d*",
                "",
                answer,
            ).strip()
            return q_text, answer

    # No separator found — no answer revealed
    return cleaned, ""


# ─────────────────────────────────────────────────────────────────────────────
async def scrape_pack(pack_url: str, browser: Browser) -> int:
    """
    Scrape one pack. Returns the number of newly saved questions.
    """
    page = await browser.new_page()
    try:
        logger.info("Navigating to %s …", pack_url)
        await page.goto(pack_url, wait_until="networkidle", timeout=60_000)

        await _reveal_answers(page)
        # Give the page a moment to fully render answers
        await page.wait_for_timeout(1500)

        questions = await _parse_pack_page(page, pack_url)
        if not questions:
            logger.warning("No questions found at %s", pack_url)
            return 0

        inserted = await upsert_questions(questions)
        logger.info("Saved %d new questions from %s", inserted, pack_url)
        return inserted
    finally:
        await page.close()


async def scrape_all_packs(
    start_id: int = 1,
    end_id: int = FIRST_PACK_ID,
    delay_sec: float = 2.0,
) -> int:
    """
    Scrape packs from start_id to end_id (inclusive, oldest-first).
    Yields total inserted count.
    """
    total = 0
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        for pack_id in range(start_id, end_id + 1):
            url = f"{BASE_URL}/pack/{pack_id}"
            try:
                inserted = await scrape_pack(url, browser)
                total += inserted
            except Exception as exc:
                logger.error("Failed scraping pack %d: %s", pack_id, exc)
            await asyncio.sleep(delay_sec)
        await browser.close()
    return total


async def scrape_first_pack() -> int:
    """Convenience wrapper to scrape only Балканфест-2025 (pack/6705)."""
    pack_url = f"{BASE_URL}/pack/{FIRST_PACK_ID}"
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            return await scrape_pack(pack_url, browser)
        finally:
            await browser.close()
