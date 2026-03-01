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
async def _dismiss_cookie_banner(page: Page) -> None:
    """Close the cookie consent banner so it doesn't block clicks."""
    for label in ("Отклонить", "Принять"):
        try:
            btn = page.locator(f"text={label}").first
            if await btn.count() > 0:
                await btn.click(timeout=3000)
                await page.wait_for_timeout(400)
                logger.debug("Dismissed cookie banner ('%s').", label)
                return
        except Exception:
            pass


async def _js_click(page: Page, locator) -> None:
    """Click via JavaScript to bypass any overlaying elements."""
    el = await locator.element_handle(timeout=5000)
    if el:
        await page.evaluate("el => el.click()", el)


async def _reveal_answers(page: Page) -> None:
    """Click the global eye-toggle (visibility_off) to reveal all answers."""
    await _dismiss_cookie_banner(page)
    try:
        toggle = page.locator("text=visibility_off").first
        if await toggle.count() > 0:
            await _js_click(page, toggle)
            await page.wait_for_timeout(800)
            logger.debug("Clicked visibility_off toggle (show all answers).")
            return
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

        # Dismiss cookie banner first, then reveal answer via JS click
        await _dismiss_cookie_banner(q_page)
        show_btn = q_page.locator("text=Показать ответ").first
        if await show_btn.count():
            await _js_click(q_page, show_btn)
            # Wait until the button text changes to "Скрыть ответ" (answer revealed)
            try:
                await q_page.locator("text=Скрыть ответ").first.wait_for(
                    state="visible", timeout=5000
                )
            except Exception:
                # Answer reveal may have worked anyway; proceed
                await q_page.wait_for_timeout(800)

        # Remove nav / header / footer DOM elements so their icon-name text
        # ("search", "quiz", "casino" …) doesn't pollute extracted content.
        # NOTE: only remove safe, clearly-non-content elements.
        await q_page.evaluate("""
            () => {
                ['nav', 'footer', 'mat-toolbar',
                 '[class*="cookie"]', '[class*="consent"]', '[class*="banner"]']
                    .forEach(sel =>
                        document.querySelectorAll(sel).forEach(el => el.remove())
                    );
            }
        """)

        # Collect content image URL (Раздаточный материал), if any.
        # Look for <img> tags inside the content area, skip tiny icons.
        image_url: str | None = None
        try:
            imgs = await q_page.locator("main img, article img, [class*='question'] img").all()
            for img in imgs:
                src = await img.get_attribute("src") or ""
                if not src or src.startswith("data:"):
                    continue
                # Skip tiny icon images
                w = await img.get_attribute("width") or ""
                h = await img.get_attribute("height") or ""
                if w and int(w) < 64:
                    continue
                if h and int(h) < 64:
                    continue
                if not src.startswith("http"):
                    src = BASE_URL + src if src.startswith("/") else BASE_URL + "/" + src
                image_url = src
                break
        except Exception as exc:
            logger.debug("Image extraction failed at %s: %s", question_url, exc)

        # Get page text — try scoped content area first, fallback to body
        content_sel = q_page.locator("main, article, [class*='question']").first
        if await content_sel.count():
            body_text = await content_sel.inner_text()
        else:
            body_text = await q_page.locator("body").inner_text()
        q_text, answer_text = _split_question_answer(body_text, "")

        # Cleanup: remove navigation / footer noise
        q_text = _clean_text(q_text)
        answer_text = _clean_text(answer_text)

        if not q_text:
            logger.warning(
                "No question text found at %s | raw snippet: %r",
                question_url,
                body_text[:300],
            )
            return None

        return {
            "pack_name": pack_name,
            "pack_link": pack_url,
            "question_number": doc_q_number,
            "text": q_text,
            "answer": answer_text or None,
            "image_url": image_url,
            "link": question_url,
            "is_sent": False,
        }
    finally:
        await q_page.close()


# Material icon ligature names rendered as text by the website's nav
_NAV_ICON_WORDS: frozenset[str] = frozenset({
    "search", "quiz", "casino", "group", "timer", "help_center",
    "dark_mode", "bug_report", "login", "logout", "content_copy",
    "thumb_up", "thumb_down", "bookmark_border", "bookmark", "share",
    "expand_more", "expand_less", "visibility_off", "visibility",
    "more_vert", "more_horiz", "menu", "close", "arrow_back",
    "arrow_forward", "home", "person", "settings", "info",
})

_NOISE_PHRASES: list[str] = [
    "Показать ответ", "Скрыть ответ",
    "Поиск", "Пакеты", "Случайный пакет", "Люди", "Таймер", "О сайте",
    "Есть вопросы? у нас есть",
    "Обратная связь", "Политика конфиденциальности", "Лицензирование",
    "Хотите печенья?", "ОтклонитьПринять", "Отклонить", "Принять",
]


def _clean_text(text: str) -> str:
    """Remove UI noise (icon names, nav items, author lines) from extracted text."""
    for phrase in _NOISE_PHRASES:
        text = text.replace(phrase, " ")

    cleaned: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Skip bare Material Icon names
        if line.lower() in _NAV_ICON_WORDS:
            continue
        # Skip "Вопрос N" header (already stored as question_number)
        if re.fullmatch(r'Вопрос\s+\d+', line):
            continue
        # Skip "Тур N" lines
        if re.fullmatch(r'Тур\s+\d+', line, re.IGNORECASE):
            continue
        # Skip author attribution lines starting with «·»
        if line.startswith("·"):
            continue
        # Skip pack-header lines like "Балканфест — 2025 · ноябрь 2025"
        # or "Куб-7. Март (2022/2023) · апр. 2023"
        if re.search(r'·\s+\S+\s+\d{4}', line):
            continue
        # Skip author lines: "Автор: Имя Фамилия"
        if re.match(r'Авто?р\s*:', line, re.IGNORECASE):
            continue
        # Skip source/citation lines: "Источники:", "1. ...", "2. https://..."
        if re.match(r'Источники\s*:', line, re.IGNORECASE):
            continue
        if re.match(r'\d+\.\s+https?://', line):
            continue
        # Skip bare vote/difficulty stats: "98", "/ 154 · 63.64%"
        if re.fullmatch(r'\d+', line):
            continue
        if re.match(r'/\s*\d+\s*[·•]', line):
            continue
        cleaned.append(line)

    result = "\n".join(cleaned)
    result = re.sub(r'\n{3,}', '\n\n', result).strip()
    return result[:3000]


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
