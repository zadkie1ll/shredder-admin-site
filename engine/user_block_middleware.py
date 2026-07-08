import logging

from django.contrib.auth import logout

from database import session_factory
from engine.user_block import ACCOUNT_BLOCKED_MESSAGE
from engine.user_block import is_user_blocked


class UserBlockMiddleware:
    """Разлогинивает полностью заблокированного пользователя (user_blocks) и
    показывает страницу логина с сообщением о блокировке. Ставится после
    AuthenticationMiddleware, поэтому покрывает все view кабинета."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            # Fail-open: ошибка проверки не должна уронить сайт для всех.
            try:
                db_session = session_factory()
                try:
                    blocked = is_user_blocked(db_session, user.id)
                finally:
                    db_session.close()
            except Exception:
                logging.exception("user block check failed, letting request through")
                blocked = False

            if blocked:
                logging.warning(
                    "blocked user %s tried to access %s, logging out",
                    user.id,
                    request.path,
                )
                logout(request)
                # Локальный импорт: engine.views тяжёлый и сам через url-роутинг
                # зависит от middleware, на уровне модуля был бы циклический импорт.
                from engine.views import render_login

                return render_login(
                    request, {"error": ACCOUNT_BLOCKED_MESSAGE}, status=403
                )

        return self.get_response(request)
