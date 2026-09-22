"""WSGI entry point for the standalone Shredder administration service."""

import os

from django.core.wsgi import get_wsgi_application


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "web_app.admin_settings")

application = get_wsgi_application()
