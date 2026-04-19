FROM python:3.12-slim

# Устанавливаем системные зависимости для работы с БД
RUN apt-get update && apt-get install -y libpq-dev gcc && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Ставим зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем проект
COPY . .

# Собираем статику (CSS/JS)
RUN python manage.py collectstatic --noinput

# Запускаем через gunicorn (в продакшене встроенный сервер Django использовать нельзя!)
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "web_app.wsgi:application"]