"""Short-lived aggregate report cache. Call only AFTER authorization/validation."""
import hashlib
import json
from threading import Lock
from django.core.cache import cache
from django.http import HttpResponse

_locks = [Lock() for _ in range(32)]


def report_response(kind, parameters, build_response, ttl=30):
    key = 'website-report-v1:' + hashlib.sha256(json.dumps([kind, parameters], default=str, sort_keys=True).encode()).hexdigest()
    try:
        cached = cache.get(key)
    except Exception:
        cached = None
    if cached is not None:
        response = HttpResponse(cached, content_type='application/json')
        response['X-Report-Cache'] = 'hit'
    else:
        with _locks[hash(key) % len(_locks)]:
            try:
                cached = cache.get(key)
            except Exception:
                cached = None
            response = HttpResponse(cached, content_type='application/json') if cached is not None else build_response()
            if cached is None and response.status_code == 200:
                try:
                    cache.set(key, response.content, timeout=ttl)
                except Exception:
                    pass  # Cache failure must not suppress the report.
            response['X-Report-Cache'] = 'hit' if cached is not None else 'miss'
    response['Cache-Control'] = 'private, no-store'
    response['X-Report-Max-Age'] = str(ttl)
    return response
