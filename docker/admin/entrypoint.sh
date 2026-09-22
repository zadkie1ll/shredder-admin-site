#!/bin/sh
set -eu

python manage.py migrate --noinput --settings web_app.admin_settings
python manage.py collectstatic --noinput --settings web_app.admin_settings

exec gunicorn \
  --bind "0.0.0.0:${SHREDDER_ADMIN_SITE_PORT:-8016}" \
  --workers "${SHREDDER_ADMIN_SITE_WORKERS:-2}" \
  --threads "${SHREDDER_ADMIN_SITE_THREADS:-4}" \
  --timeout "${SHREDDER_ADMIN_SITE_TIMEOUT:-60}" \
  web_app.admin_wsgi:application
