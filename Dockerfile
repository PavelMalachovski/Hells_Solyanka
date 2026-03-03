FROM python:3.12-slim

WORKDIR /app

# All system packages required by Playwright's Chromium headless shell.
# We install deps manually instead of `playwright install-deps` because
# Debian Trixie no longer ships ttf-unifont / ttf-ubuntu-font-family.
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Chromium core
    libnss3 \
    libnspr4 \
    libdbus-1-3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libxkbcommon0 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2t64 \
    libatspi2.0-0 \
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxext6 \
    libxfont2 \
    libxi6 \
    # Fonts (Trixie-compatible replacements)
    fonts-unifont \
    fonts-liberation \
    fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Download Chromium headless shell (no install-deps needed — done above)
RUN playwright install chromium

COPY *.py ./
COPY webapp/ ./webapp/

ENV PYTHONUNBUFFERED=1
ENV TZ=Europe/Prague

CMD ["python", "main.py"]
