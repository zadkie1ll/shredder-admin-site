"""Шифрование ссылки подписки для приложения Incy (incy://crypt1/...).

Синхронный порт utils/encrypt_incy_url.py из телеграм-бота: тот же
node-скрипт incy_link_encoder.mjs с официальным пакетом @incy/link-encoder
(свой формат crypt1, на Python не переносится). Требует Node.js и
node_modules в образе сайта (см. docker/website/Dockerfile и package.json).

Ссылка детерминированной не считается, но для одного subscription_url
результат переиспользуем через LRU-кэш — дашборд рендерится часто, а
subprocess стоит ~100 мс.
"""

import json
import logging
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

INCY_LINK_PREFIX = "incy://crypt1/"
INCY_PROVIDER_NAME = "Monkey Island VPN"
INCY_ENCODER_TIMEOUT_SECONDS = 3
INCY_ENCODER_SCRIPT = Path(__file__).with_name("incy_link_encoder.mjs")
INCY_ENCODER_PACKAGE = (
    INCY_ENCODER_SCRIPT.parent.parent
    / "node_modules"
    / "@incy"
    / "link-encoder"
    / "package.json"
)


class IncyEncoderError(RuntimeError):
    pass


def _encoder_failure_message(stderr: bytes) -> str:
    error_output = stderr.decode("utf-8", errors="replace")
    if "ERR_MODULE_NOT_FOUND" in error_output and "@incy/link-encoder" in error_output:
        return "Incy link encoder dependency is not installed; run npm ci"
    return "Incy link encoder failed"


@lru_cache(maxsize=4096)
def encrypt_incy_url(url: str, name: str = INCY_PROVIDER_NAME) -> str:
    node_path = shutil.which("node")
    if node_path is None:
        raise IncyEncoderError("Node.js executable not found")

    if not INCY_ENCODER_PACKAGE.exists():
        raise IncyEncoderError(
            "Incy link encoder dependency is not installed; run npm ci"
        )

    payload = json.dumps({"url": url, "name": name}).encode("utf-8")

    try:
        completed = subprocess.run(
            [node_path, str(INCY_ENCODER_SCRIPT)],
            input=payload,
            capture_output=True,
            timeout=INCY_ENCODER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise IncyEncoderError("Incy link encoder timed out") from exc

    if completed.returncode != 0:
        raise IncyEncoderError(_encoder_failure_message(completed.stderr))

    encrypted_link = completed.stdout.decode("utf-8").strip()
    if not encrypted_link.startswith(INCY_LINK_PREFIX):
        raise IncyEncoderError("Unexpected Incy link format")

    return encrypted_link
