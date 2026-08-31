#!/bin/sh
set -e

python manage.py migrate --noinput
python manage.py collectstatic --noinput

# threads > 1 автоматически включает worker-class gthread: медленный запрос
# (тяжёлый отчёт админки) занимает поток, а не целый процесс — остальные
# запросы, включая кабинет клиентов, продолжают обслуживаться
exec gunicorn \
  --bind 0.0.0.0:8000 \
  --workers "${GUNICORN_WORKERS:-3}" \
  --threads "${GUNICORN_THREADS:-4}" \
  --timeout "${GUNICORN_TIMEOUT:-60}" \
  web_app.wsgi:application
