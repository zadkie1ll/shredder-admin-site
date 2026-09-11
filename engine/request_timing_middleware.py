import logging
from time import perf_counter
from uuid import uuid4

from django.conf import settings
from engine import sql_timing


log = logging.getLogger("engine.request_timing")


class RequestTimingMiddleware:
    """Emit safe per-route latency records without paths, queries, or tokens."""

    def __init__(self, get_response):
        self.get_response = get_response
        sql_timing.install()

    def __call__(self, request):
        request_id = uuid4().hex
        started = perf_counter()
        status = 500
        sql = {"count": 0, "ms": 0.0}
        context_token = sql_timing.current.set(sql)
        try:
            response = self.get_response(request)
            status = response.status_code
            response["X-Request-ID"] = request_id
            # Server-Timing раскрывает клиенту время SQL, а по нему можно
            # отличить ветки одинакового ответа (например, есть ли email в
            # базе у magic-link). Наружу — только по явному флагу; в лог
            # метрики пишутся всегда.
            if getattr(settings, "REQUEST_TIMING_EXPOSE_SERVER_TIMING", False):
                response["Server-Timing"] = f'sql;dur={sql["ms"]:.1f}'
            return response
        finally:
            sql_timing.current.reset(context_token)
            resolver_match = getattr(request, "resolver_match", None)
            route = getattr(resolver_match, "route", None) or "unresolved"
            duration_ms = (perf_counter() - started) * 1000
            slow_ms = float(getattr(settings, "REQUEST_TIMING_SLOW_MS", 1000))
            if (
                getattr(settings, "REQUEST_TIMING_LOG_ALL", False)
                or status >= 500
                or duration_ms >= slow_ms
            ):
                log.info(
                    "request_complete request_id=%s method=%s route=%s status=%s duration_ms=%.1f sql_count=%s sql_ms=%.1f",
                    request_id,
                    getattr(request, "method", "UNKNOWN"),
                    route,
                    status,
                    duration_ms,
                    sql["count"],
                    sql["ms"],
                )
