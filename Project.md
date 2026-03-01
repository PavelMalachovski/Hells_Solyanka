# Project: Telegram Question Parser Bot (Railway Edition)

## Description
Асинхронный Telegram-бот на Python для парсинга вопросов с `https://gotquestions.online/`, сохранения их в базу данных PostgreSQL и автоматической рассылки в группу по расписанию (раз в час с 09:00 до 20:00).

## Tech Stack
- **Python 3.12+**
- **aiogram 3.x** (Telegram Bot API)
- **Playwright** (Scraping)
- **SQLAlchemy + asyncpg** (Database ORM)
- **PostgreSQL** (Managed on Railway)
- **APScheduler** (Task Scheduling)
- **Docker** (Deployment)

## Infrastructure & Hosting (Railway)
- Бот разворачивается через **Dockerfile** (необходим для установки зависимостей Chromium).
- **PostgreSQL**: Использовать внутреннюю переменную Railway `${{Postgres.DATABASE_URL}}`.
- **Timezone**: Настроить `TZ=Europe/Prague` в переменных окружения Railway для корректной работы расписания.

## Business Logic
1. **Database Schema**:
   - Таблица `questions`: `id` (PK), `text` (String), `link` (String, Unique), `is_sent` (Boolean, Default=False), `created_at` (DateTime).
2. **Scraper**:
   - Запускается по команде `/parse` или при пустой базе.
   - Использует Playwright для извлечения вопросов с сайта.
   - Делает "UPSERT" (вставляет только новые вопросы, игнорируя существующие ссылки).
3. **Scheduler**:
   - Работает ежедневно.
   - Интервал: `09:00 - 20:00` (каждый час, например, в `00` минут).
   - Выбирает один случайный вопрос, где `is_sent == False`, отправляет в `GROUP_ID` и ставит `is_sent = True`.

## Docker Configuration Details
Для корректной работы на Railway Dockerfile должен:
1. Использовать официальный образ Python.
2. Устанавливать системные пакеты: `libnss3`, `libnspr4`, `libgbm1`, `libasound2` и др.
3. Выполнять `playwright install chromium` и `playwright install-deps`.

## Project Structure
- `main.py` — точка входа, инициализация бота и планировщика.
- `database.py` — модели SQLAlchemy и подключение к БД.
- `scraper.py` — логика парсинга через Playwright.
- `scheduler.py` — логика периодических задач.
- `Dockerfile` — конфигурация контейнера.
- `.env` — локальные переменные (BOT_TOKEN, GROUP_ID, DATABASE_URL).

## TODO List
- [ ] Создать асинхронную модель БД (SQLAlchemy 2.0 style).
- [ ] Написать парсер, который корректно находит вопросы на `gotquestions.online`.
- [ ] Настроить `AsyncIOScheduler` для отправки сообщений с 9:00 до 20:00.
- [ ] Подготовить `Dockerfile` на базе `python:3.12-slim` с установкой браузеров Playwright.
- [ ] Добавить обработку случая, когда вопросы в базе закончились (уведомление админа).
