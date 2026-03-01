# Hells Solyanka — Telegram Question Bot

Асинхронный Telegram-бот, который парсит вопросы с [gotquestions.online](https://gotquestions.online/), сохраняет их в PostgreSQL и отправляет в Telegram-группу каждый час с 09:00 до 20:00 (Europe/Prague).

---

## Быстрый старт (локально)

### 1. Требования
- Python 3.12+
- PostgreSQL (локально или любой managed-сервис)
- Docker (опционально, для деплоя)

### 2. Установка

```bash
pip install -r requirements.txt
playwright install chromium
playwright install-deps chromium
```

### 3. Конфигурация

Скопируй `.env.example` → `.env` и заполни переменные:

| Переменная    | Описание |
|---------------|----------|
| `BOT_TOKEN`   | Токен бота от @BotFather |
| `GROUP_ID`    | ID группы/канала, куда отправлять вопросы |
| `ADMIN_ID`    | Твой Telegram user ID (для admin-команд) |
| `DATABASE_URL`| PostgreSQL URL (Railway автоматом даёт через `${{Postgres.DATABASE_URL}}`) |
| `PACK_ID`     | ID пакета для первичного парсинга (по умолчанию **6705** = Балканфест-2025) |

### 4. Запуск

```bash
# Загрузить переменные из .env (Linux/macOS):
export $(grep -v '^#' .env | xargs)

# Windows PowerShell:
# Get-Content .env | ForEach-Object { if ($_ -notmatch '^#' -and $_ -ne '') { $s = $_.Split('=',2); [System.Environment]::SetEnvironmentVariable($s[0], $s[1]) } }

python main.py
```

При первом запуске:
1. Создаётся таблица `questions` в БД.
2. Автоматически запускается парсинг **Балканфест-2025** (если БД пустая).
3. Запускается планировщик (09–20 по Праге, каждый час).

---

## Команды бота

| Команда | Кто | Описание |
|---------|-----|----------|
| `/start` | все | Приветствие + статистика |
| `/status` | все | Сколько вопросов в базе |
| `/parse` | admin | Запарсить **Балканфест-2025** |
| `/parse_all` | admin | Запарсить все 6 550 пакетов (долго!) |
| `/send_now` | admin | Немедленно отправить один вопрос в группу |

---

## Формат сообщения в группе

```
📚 Балканфест — 2025  |  Вопрос 1
──────────────────────────────

На мастер-классе веганского фестиваля в городке Книч можно буквально научиться
невозможному. Ответьте пятью словами: чему именно?

💡 Ответ: [скрыт спойлером — нажми чтобы открыть]
```

Ответ скрыт тегом `<tg-spoiler>` — участники видят его только тапнув.

---

## Деплой на Railway

1. Создай новый проект на [Railway](https://railway.app/).
2. Добавь сервис **PostgreSQL** — Railway автоматически добавит `DATABASE_URL`.
3. Добавь сервис из этого GitHub репозитория.
4. В переменных окружения пропиши:
   - `BOT_TOKEN`, `GROUP_ID`, `ADMIN_ID`, `PACK_ID`
   - `DATABASE_URL` → `${{Postgres.DATABASE_URL}}`
   - `TZ` → `Europe/Prague`
5. Railway сам соберёт Docker-образ по `Dockerfile` и задеплоит.

---

## Структура проекта

```
├── main.py          # Точка входа: бот + планировщик
├── database.py      # SQLAlchemy 2.0 модели и хелперы
├── scraper.py       # Playwright-парсер gotquestions.online
├── scheduler.py     # APScheduler (09-20, каждый час)
├── Dockerfile       # Для Railway / любого Docker-хостинга
├── requirements.txt
├── .env.example
└── README.md
```

---

## Примечания

- **Порядок пакетов**: сначала Балканфест-2025 (свежий), потом `/parse_all` идёт с pack/1 по pack/6705.
- **UPSERT**: повторный запуск `/parse` безопасен — дублей не будет.
- **Случайный вопрос**: каждый раз выбирается случайный из ещё не отправленных.
- **Уведомление о конце базы**: когда вопросы заканчиваются, бот пишет тебе в ЛС.
