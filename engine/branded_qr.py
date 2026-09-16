"""Брендовый QR-код Monkey Island в «плиточном» стиле (порт utils/branded_qr.py бота
для кабинета сайта: QR ссылки подписки и реферальной ссылки): жёлтые скруглённые
точки-модули на чёрном фоне, жёлтая скруглённая рамка и плоская обезьянка
(эмблема images/qr-logo.png — перекрашенный в жёлтый аватар бота) в центре.

Рендер полностью кастомный (PIL по матрице qrcode): готовые стили qrcode
«точечную» сетку с рамкой и сплошными финдерами не умеют. Требования
сканируемости, которые нельзя нарушать при правках (проверены zxing-cpp —
движок класса реальных сканеров телефонов, читает и светлое-на-тёмном):

- финдер-паттерны (три угловых квадрата 7x7) рисуются СПЛОШНЫМИ
  (кольцо + центр 3x3): собранные из точек с зазорами финдеры не
  детектируются вовсе;
- коррекция ошибок H (~30%) — её бюджет тратят зазоры модулей и
  центральная эмблема (~9% площади);
- эмблема не больше EMBLEM_FRACTION ширины кода.

Гард — tests/test_branded_qr.py: декодирует итоговую картинку zxing-cpp,
включая уменьшенную копию.

При любой ошибке стилизации функция откатывается на нейтральный
qrcode.make() — реферальный QR важнее бренда, экран «Пригласить друга»
не должен ломаться.
"""

import logging
from pathlib import Path

import qrcode
from PIL import Image, ImageDraw

QR_EMBLEM_PATH = Path(__file__).resolve().parent / "static" / "icons" / "qr-logo.png"

# Фирменные цвета: жёлтые модули на чёрном (как в остальном бренд-стиле).
QR_FRONT_COLOR = (255, 204, 0)
QR_BACK_COLOR = (10, 10, 10)

BOX = 24            # размер модуля, px
DOT = 18            # видимая часть модуля (зазоры дают «точечную» сетку)
DOT_RADIUS = 6      # скругление точки
QUIET_MODULES = 3   # тихая зона внутри рамки, в модулях
FRAME_WIDTH = 22    # толщина жёлтой рамки
FRAME_PAD = 18      # отступ рамки от края картинки
FRAME_RADIUS = 110  # скругление рамки
EMBLEM_FRACTION = 0.30  # ширина эмблемы относительно матрицы


def _draw_finder(draw: ImageDraw.ImageDraw, x0: int, y0: int) -> None:
    """Сплошной финдер 7x7: скруглённое кольцо толщиной в модуль + центр 3x3."""
    x1, y1 = x0 + 7 * BOX, y0 + 7 * BOX
    draw.rounded_rectangle(
        (x0, y0, x1 - 1, y1 - 1), radius=42, outline=QR_FRONT_COLOR, width=BOX
    )
    cx0, cy0 = x0 + 2 * BOX, y0 + 2 * BOX
    draw.rounded_rectangle(
        (cx0, cy0, cx0 + 3 * BOX - 1, cy0 + 3 * BOX - 1),
        radius=24,
        fill=QR_FRONT_COLOR,
    )


def make_branded_qr(data: str):
    """Возвращает PIL-изображение брендового QR для переданной строки."""
    try:
        qr = qrcode.QRCode(
            error_correction=qrcode.constants.ERROR_CORRECT_H, border=0
        )
        qr.add_data(data)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        n = len(matrix)

        quiet = QUIET_MODULES * BOX
        size = n * BOX + 2 * quiet
        width = size + 2 * (FRAME_WIDTH + FRAME_PAD)
        img = Image.new("RGB", (width, width), QR_BACK_COLOR)
        draw = ImageDraw.Draw(img)

        draw.rounded_rectangle(
            (FRAME_PAD, FRAME_PAD, width - 1 - FRAME_PAD, width - 1 - FRAME_PAD),
            radius=FRAME_RADIUS,
            outline=QR_FRONT_COLOR,
            width=FRAME_WIDTH,
        )

        origin = FRAME_WIDTH + FRAME_PAD + quiet
        finders = [(0, 0), (n - 7, 0), (0, n - 7)]

        def in_finder(col: int, row: int) -> bool:
            return any(
                fc <= col < fc + 7 and fr <= row < fr + 7 for fc, fr in finders
            )

        emblem_modules = int(n * EMBLEM_FRACTION)
        e0 = (n - emblem_modules) / 2
        e1 = e0 + emblem_modules

        def in_emblem(col: int, row: int) -> bool:
            return e0 <= col < e1 and e0 <= row < e1

        pad = (BOX - DOT) / 2
        for row in range(n):
            for col in range(n):
                if not matrix[row][col] or in_finder(col, row) or in_emblem(col, row):
                    continue
                x = origin + col * BOX + pad
                y = origin + row * BOX + pad
                draw.rounded_rectangle(
                    (x, y, x + DOT - 1, y + DOT - 1),
                    radius=DOT_RADIUS,
                    fill=QR_FRONT_COLOR,
                )

        for fc, fr in finders:
            _draw_finder(draw, origin + fc * BOX, origin + fr * BOX)

        emblem = Image.open(QR_EMBLEM_PATH).convert("RGBA")
        emblem_size = int(emblem_modules * BOX * 0.96)
        emblem = emblem.resize((emblem_size, emblem_size), Image.LANCZOS)
        pos = (width - emblem_size) // 2
        img.paste(emblem, (pos, pos), emblem)

        return img
    except Exception as e:
        logging.exception(f"branded qr generation failed, falling back to plain: {e}")
        return qrcode.make(data)
