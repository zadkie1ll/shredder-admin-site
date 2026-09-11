FROM python:3.12-slim AS builder

WORKDIR /app

# Переменные для Python и pip
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venv/bin:$PATH"

# Компилятор и dev-зависимости нужны только на этапе сборки; nodejs+npm —
# для установки @incy/link-encoder (шифрование incy://-ссылок подписки)
RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev nodejs npm \
    && python -m venv /opt/venv \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY package.json package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force

FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venv/bin:$PATH"

# В рантайме: клиент Postgres и nodejs (запуск incy_link_encoder.mjs —
# шифрование ссылок подписки для приложения Incy)
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 nodejs \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app/node_modules ./node_modules

# Копируем только то, что действительно нужно приложению в рантайме
COPY manage.py database.py ./
COPY common ./common
COPY engine ./engine
COPY mobile_api ./mobile_api
COPY proto ./proto
COPY web_app ./web_app
COPY docker/website/entrypoint.sh /entrypoint.sh

RUN mkdir -p /app/staticfiles \
    && chmod +x /entrypoint.sh

EXPOSE 8000

CMD ["/entrypoint.sh"]
