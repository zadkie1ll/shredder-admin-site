# Monkey Island Website

Django-приложение для продажи VPN-подписок Monkey Island (xray/VLESS TCP Reality через remnawave панель).

---

## Структура проекта

```
engine/           — основное Django-приложение (views, models, urls)
  templates/      — HTML-шаблоны страниц
web_app/          — конфиг Django (settings, urls, wsgi/asgi)
common/           — общие утилиты (rmws_client и др.)
proto/            — gRPC protobuf-схемы для remnawave
docker/           — Docker-конфиги для деплоя (website, edge, postgres)
```

---

## Страницы и их логика

### `payment_status.html` — Статус платежа

Страница опрашивает backend по API (`status_api_url`) каждые 2.5 секунды и отображает одно из трёх состояний:

| Состояние | `data-status` | Поведение |
|-----------|---------------|-----------|
| **pending** | `pending` | Анимированный орбитальный индикатор + шаги процесса |
| **succeeded** | `succeeded` | Зелёный чекмарк, редирект в ЛК через 1.2 с |
| **failed** | `failed` | Красный индикатор, сообщение об ошибке |

**Логика polling:**
```js
async function pollStatus() {
    // GET status_api_url + window.location.search
    // При pending → повторить через 2500ms
    // При succeeded/failed → остановить опрос
    // При сетевой ошибке → повторить через 3500ms
}
```

**Контекстные переменные Django:**
- `initial_status` — начальное состояние (`pending` / `succeeded` / `failed`)
- `initial_message` — сообщение об ошибке (для failed)
- `login_url` — URL личного кабинета
- `status_api_url` — API-эндпоинт для polling
- `support_telegram_url` — ссылка на Telegram-поддержку

#### Состояние pending — орбитальный индикатор

Вместо простого спиннера используется многослойная анимация:
- **`.orb-ring-outer`** — внешнее кольцо, вращается по часовой (1.6 с, eased)
- **`.orb-ring-inner`** — внутреннее кольцо, вращается против часовой (1.1 с, linear)
- **`.orb-pulse`** — пульсирующий ореол, затухает наружу (2.2 с)
- **`.orb-center`** — центральный круг с "дыханием" (scale 1→1.05, 2.8 с)
- Шаги процесса: «Оплата отправлена ✓» → «Ожидаем подтверждения ⟳» → «Активация подписки»
- Прогресс-точки (4 штуки, волновая анимация)

При переходе в `succeeded` кольца останавливаются, центр становится зелёным с `✓`.
При переходе в `failed` кольца останавливаются, центр становится красным с `!`.

---

### `dashboard_v2.html` — Личный кабинет

SPA-like дашборд с вкладками (home / subscription / setup / profile / support).
Адаптивный: десктоп — боковое меню 248px, мобайл — нижний nav-bar.

**Вкладки:**
- **home** — статус подписки, прогресс-бар, быстрые действия
- **subscription** — ключи подключения, QR-коды
- **setup** — пошаговый мастер подключения по платформам
- **profile** — реферальная программа
- **support** — чат поддержки

**Контекстные переменные Django:** `user`, `seconds_left`, `time_left_value`, `time_left_label`, `show_*_banner`, `tg_bind_link`, `has_recurrent` и др.

---

### `login.html` — Вход через magic link / Telegram

### `index_vpn.html` — Лендинг VPN-подписок

### `offer.html` — Публичная оферта

---

## Деплой

Смотри `docker/website/PRODUCTION.md` и `docker/edge/PRODUCTION.md`.

Конфиги: `docker/website/docker-compose.yml` + `nginx.conf.template`.
