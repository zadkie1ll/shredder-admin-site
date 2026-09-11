"""Request-scoped SQL execution totals; statements and parameters are never logged."""
from contextvars import ContextVar
from time import perf_counter
from sqlalchemy import event
from sqlalchemy.engine import Engine

current = ContextVar('website_sql_timing', default=None)


def before_cursor(conn, cursor, statement, parameters, context, executemany):
    if current.get() is not None:
        context._website_started = perf_counter()


def after_cursor(conn, cursor, statement, parameters, context, executemany):
    metric = current.get()
    started = getattr(context, '_website_started', None)
    if metric is not None and started is not None:
        metric['count'] += 1
        metric['ms'] += (perf_counter() - started) * 1000


def install():
    if not event.contains(Engine, 'before_cursor_execute', before_cursor):
        event.listen(Engine, 'before_cursor_execute', before_cursor)
        event.listen(Engine, 'after_cursor_execute', after_cursor)
