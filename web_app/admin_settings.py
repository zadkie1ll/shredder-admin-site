"""Settings overlay for running only the Shredder administration surface.

The original application uses generic variable names.  The standalone service
accepts Shredder-prefixed names and maps them before loading the shared settings
module, so it can coexist with the rest of the local infrastructure.
"""

import os


_ENV_ALIASES = {
    "SECRET_KEY": "SHREDDER_ADMIN_SITE_SECRET_KEY",
    "DEBUG": "SHREDDER_ADMIN_SITE_DEBUG",
    "ALLOWED_HOSTS": "SHREDDER_ADMIN_SITE_ALLOWED_HOSTS",
    "CSRF_TRUSTED_ORIGINS": "SHREDDER_ADMIN_SITE_CSRF_TRUSTED_ORIGINS",
    "WEB_DATABASE_URL": "SHREDDER_ADMIN_SITE_WEB_DATABASE_URL",
    "SERVICE_DATABASE_URL": "SHREDDER_ADMIN_DATABASE_URL",
    "CACHE_REDIS_URL": "SHREDDER_ADMIN_REDIS_URL",
    "RWMS_HOST": "SHREDDER_ADMIN_RWMS_HOST",
    "RWMS_PORT": "SHREDDER_ADMIN_RWMS_PORT",
    "YOOKASSA_SHOP_ID": "SHREDDER_ADMIN_YOOKASSA_SHOP_ID",
    "YOOKASSA_SECRET_KEY": "SHREDDER_ADMIN_YOOKASSA_SECRET",
    "SUPPORT_ADMIN_PASSWORD": "SHREDDER_ADMIN_SITE_PASSWORD",
    "SUPPORT_STAFF_PASSWORD": "SHREDDER_ADMIN_SITE_SUPPORT_PASSWORD",
    "BOT_REDIS_HOST": "SHREDDER_ADMIN_BOT_REDIS_HOST",
    "BOT_REDIS_PORT": "SHREDDER_ADMIN_BOT_REDIS_PORT",
    "BOT_REDIS_PASSWORD": "SHREDDER_ADMIN_BOT_REDIS_PASSWORD",
    "BOT_REDIS_QUEUES": "SHREDDER_ADMIN_BOT_REDIS_QUEUES",
}

for _standard_name, _shredder_name in _ENV_ALIASES.items():
    _value = os.getenv(_shredder_name)
    if _value is not None:
        # An explicitly scoped Shredder value wins over generic variables that
        # may already be present in a shared host environment (for example a
        # global DEBUG value used by another service).
        os.environ[_standard_name] = _value

# These settings are mandatory in the combined customer website, but optional
# for an admin-only process. Empty provider credentials keep the corresponding
# actions unavailable without inventing production secrets.
_ADMIN_ONLY_DEFAULTS = {
    "PAYMENT_GATEWAY": "yookassa",
    "WATA_HOST": "",
    "WATA_TOKEN": "",
    "YOOKASSA_SHOP_ID": "",
    "YOOKASSA_SECRET_KEY": "",
    "EMAIL_HOST": "",
    "EMAIL_PORT": "25",
    "EMAIL_HOST_USER": "",
    "EMAIL_HOST_PASSWORD": "",
    "RWMS_HOST": "127.0.0.1",
    "RWMS_PORT": "50051",
}
for _name, _default in _ADMIN_ONLY_DEFAULTS.items():
    os.environ.setdefault(_name, _default)

from .settings import *  # noqa: E402,F403


ROOT_URLCONF = "web_app.admin_urls"
WSGI_APPLICATION = "web_app.admin_wsgi.application"

# The standalone admin neither exposes the customer mobile API nor evaluates
# customer account blocks in middleware. Administrative authorization remains
# enforced by the support-admin views and their role checks.
INSTALLED_APPS = [app for app in INSTALLED_APPS if app != "mobile_api"]  # noqa: F405
MIDDLEWARE = [
    middleware
    for middleware in MIDDLEWARE  # noqa: F405
    if middleware != "engine.user_block_middleware.UserBlockMiddleware"
]
