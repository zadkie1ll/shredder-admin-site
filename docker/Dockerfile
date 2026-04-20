FROM python:3.12-slim

# Устанавливаем системные зависимости для работы с БД
RUN apt-get update && apt-get install -y libpq-dev gcc && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Переменные для Python (не плодим .pyc файлы и сразу шлем логи в консоль)
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Ставим зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем проект
COPY . .

# Создаем папку для статики, чтобы gunicorn/nginx имели к ней доступ
RUN mkdir -p /app/staticfiles

# Выставляем порт
EXPOSE 8000

# Запускаем через gunicorn (в продакшене встроенный сервер Django использовать нельзя!)
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "web_app.wsgi:application"]