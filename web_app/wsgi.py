"""
WSGI config for web_app project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.0/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "web_app.settings")

application = get_wsgi_application()

# Фоновый воркер «Замеров ТСПУ» стартует только здесь — то есть в gunicorn, а не
# при migrate/collectstatic/manage-командах (они wsgi.py не импортируют).
try:
    from engine import censor_worker

    censor_worker.start()
except Exception:  # старт воркера не должен ронять веб-приложение
    import logging

    logging.exception("censor worker: failed to start")

# Фоновый воркер «Инфраструктуры» (телеметрия нод, аномалии, автозамена IP) —
# те же правила, что и у censor_worker: только gunicorn, свой advisory lock.
try:
    from engine import infra_worker

    infra_worker.start()
except Exception:  # старт воркера не должен ронять веб-приложение
    import logging

    logging.exception("infra worker: failed to start")
