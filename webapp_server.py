"""
webapp_server.py — aiohttp web server for the Telegram Web App.

Serves:
  /              → webapp/index.html (and static assets)
  /api/stats     → question statistics
  /api/packs     → list of packs with counts
  /api/pack/<name> → questions in a specific pack
  /api/command   → execute admin commands
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from aiohttp import web

from database import (
    clear_questions,
    count_questions,
    get_packs,
    get_questions_by_pack,
)
from scraper import BASE_URL

logger = logging.getLogger(__name__)

WEBAPP_DIR = Path(__file__).parent / "webapp"
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0")) or None
PORT = int(os.environ.get("PORT", "8080"))


# ─── API handlers ────────────────────────────────────────────────────────────

async def api_stats(request: web.Request) -> web.Response:
    """Return overall question statistics."""
    total = await count_questions()
    sent = await count_questions() - await count_questions(unsent_only=True)
    pending = await count_questions(unsent_only=True)
    packs = await get_packs()
    return web.json_response({
        "total": total,
        "sent": sent,
        "pending": pending,
        "packs": len(packs),
    })


async def api_packs(request: web.Request) -> web.Response:
    """Return list of packs with question counts."""
    packs = await get_packs()
    return web.json_response({
        "packs": [
            {
                "name": p["pack_name"],
                "pack_id": p["pack_id"],
                "total": p["total"],
                "sent": p["total"] - p["unsent"],
                "unsent": p["unsent"],
            }
            for p in packs
        ]
    })


async def api_pack_questions(request: web.Request) -> web.Response:
    """Return questions for a specific pack (by name)."""
    pack_name = request.match_info["name"]
    # Find pack_link by looking up all packs
    packs = await get_packs()
    pack_link = None
    for p in packs:
        if p["pack_name"] == pack_name:
            pack_link = p["pack_link"]
            break
    if not pack_link:
        return web.json_response({"error": "Pack not found"}, status=404)

    # Get ALL questions (not paged) for the web app
    questions = await get_questions_by_pack(pack_link, page=0, unsent_only=False)
    # Fetch more pages if needed
    all_questions = list(questions)
    page = 1
    while len(questions) == 5:  # PAGE_SIZE
        questions = await get_questions_by_pack(pack_link, page=page, unsent_only=False)
        all_questions.extend(questions)
        page += 1

    return web.json_response({
        "pack_name": pack_name,
        "questions": [
            {
                "id": q.id,
                "number": q.question_number,
                "text": q.text,
                "answer": q.answer,
                "is_sent": q.is_sent,
                "image_url": q.image_url,
            }
            for q in all_questions
        ]
    })


async def api_command(request: web.Request) -> web.Response:
    """Execute admin commands via the Web App."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"ok": False, "message": "Invalid JSON"}, status=400)

    command = body.get("command", "")
    bot = request.app.get("bot")

    if command == "/send_now":
        if bot:
            from scheduler import send_question
            try:
                await send_question(bot)
                return web.json_response({"ok": True, "message": "Вопрос отправлен! 🔥"})
            except Exception as e:
                logger.exception("send_now via webapp failed")
                return web.json_response({"ok": False, "message": str(e)})
        return web.json_response({"ok": False, "message": "Бот не доступен"})

    elif command == "/parse":
        from scraper import scrape_first_pack
        try:
            inserted = await scrape_first_pack()
            return web.json_response({"ok": True, "message": f"Добавлено вопросов: {inserted}"})
        except Exception as e:
            logger.exception("parse via webapp failed")
            return web.json_response({"ok": False, "message": str(e)})

    elif command == "/clear":
        deleted = await clear_questions()
        return web.json_response({"ok": True, "message": f"Удалено вопросов: {deleted}"})

    elif command == "/status":
        total = await count_questions()
        unsent = await count_questions(unsent_only=True)
        return web.json_response({
            "ok": True,
            "message": f"Всего: {total}, ожидает: {unsent}, отправлено: {total - unsent}",
        })

    elif command == "/questions":
        packs = await get_packs()
        total_unsent = sum(p["unsent"] for p in packs)
        return web.json_response({
            "ok": True,
            "message": f"Пакетов: {len(packs)}, неотправленных: {total_unsent}",
        })

    return web.json_response({"ok": False, "message": "Неизвестная команда"}, status=400)


# ─── Static file serving ─────────────────────────────────────────────────────

async def serve_index(request: web.Request) -> web.FileResponse:
    """Serve the main webapp page."""
    index = WEBAPP_DIR / "index.html"
    if not index.exists():
        logger.error("index.html not found at %s", index)
        raise web.HTTPNotFound(text=f"index.html not found. WEBAPP_DIR={WEBAPP_DIR}, exists={WEBAPP_DIR.exists()}, contents={list(WEBAPP_DIR.iterdir()) if WEBAPP_DIR.exists() else 'N/A'}")
    return web.FileResponse(index)


# ─── App factory ─────────────────────────────────────────────────────────────

def create_webapp(bot=None) -> web.Application:
    """Create and return the aiohttp Application."""
    app = web.Application()
    app["bot"] = bot

    # API routes
    app.router.add_get("/api/stats", api_stats)
    app.router.add_get("/api/packs", api_packs)
    app.router.add_get("/api/pack/{name}", api_pack_questions)
    app.router.add_post("/api/command", api_command)

    # Serve index.html and static assets
    logger.info("WEBAPP_DIR=%s exists=%s", WEBAPP_DIR, WEBAPP_DIR.exists())
    if WEBAPP_DIR.exists():
        logger.info("WEBAPP_DIR contents: %s", list(WEBAPP_DIR.iterdir()))
    app.router.add_get("/", serve_index)
    if WEBAPP_DIR.exists():
        app.router.add_static("/static", WEBAPP_DIR)

    return app


async def start_webapp_server(bot=None) -> web.AppRunner:
    """Start the aiohttp server and return the runner (for cleanup)."""
    app = create_webapp(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info("Web App server started on port %d", PORT)
    return runner
