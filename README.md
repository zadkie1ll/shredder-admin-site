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

Пользовательские тексты в шаблонах должны оставаться профессиональными и
сервисными. Не используйте игровые, пиратские или морские метафоры в onboarding,
кабинете, login-flow, уведомлениях об оплате и ошибках; бренд Monkey Island
сохраняется как название продукта.

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

Личный кабинет поддерживает новый инвариант: запись в SQLAlchemy-таблице `users`
может существовать без RWMS-подписки. В этом случае кабинет показывает состояние
«Нет активной подписки», скрывает ключи подключения и ведёт пользователя к оплате.
После успешной оплаты платёжный сервис активирует/создаёт RWMS-подписку для того же
`users.username`, и кабинет начинает показывать ключи и мастер настройки.

В `dashboard_v2.html` аккаунт без активной подписки сначала попадает не в обычный
дашборд, а на компактный экран выбора тарифа с преимуществами и оплатой. Обычный
интерфейс кабинета, мобильное меню и реферальная плавающая кнопка в этом состоянии
скрыты.

**Вкладки:**
- **home** — статус подписки, прогресс-бар, быстрые действия
- **subscription** — ключи подключения, QR-коды
- **setup** — пошаговый мастер подключения по платформам
- **profile** — реферальная программа
- **support** — чат поддержки

В десктопном сайдбаре внизу есть отдельная кнопка «Выйти».

**Контекстные переменные Django:** `user`, `seconds_left`, `time_left_value`, `time_left_label`, `show_*_banner`, `tg_bind_link`, `has_recurrent` и др.

---

### `login.html` — Вход через magic link / Telegram

Вход на сайте может создавать локальный аккаунт без RWMS-подписки. Флаг
`SITE_TRIAL_REGISTRATION_ENABLED` больше не блокирует регистрацию на сайте: он
только включает legacy-сценарий, при котором сайт сразу пытается создать RWMS trial.
Если флаг выключен, сайт создаёт только `users` и отправляет пользователя в кабинет
без активной подписки.

### Лендинги и платный flow

`index_vpn.html`, `index_vps.html` и `index_vps_direct_sale.html` ведут нового
пользователя по прямому платному сценарию: лендинг → тарифы → email → `/pay/` →
WATA/payment status → личный кабинет. Основной CTA на лендингах ведёт к тарифам,
а вход в кабинет оставлен вторичным действием для существующих пользователей.

Перед тарифами показывается компактный trust-блок: оплата картой, доступ после
подтверждения, ссылка на email и поддержка. Формы оплаты используют
`login_link_kind=purchase_permanent`, чтобы после оплаты открыть статус платежа,
а затем залогинить пользователя постоянной purchase-ссылкой.

Минимальная воронка для аналитики:
- `landing_view` — лендинг открыт;
- `tariff_cta_click` — пользователь нажал CTA к тарифам;
- `tariff_seen` — блок тарифов попал в viewport;
- `pay_click` — форма оплаты отправлена;
- `payment_widget_loaded` — WATA-форма открылась;
- `payment_success` / `payment_failed` — финальный статус оплаты.

Backend дополнительно пишет существующие события `create_invoice_*` в `event_logs`
после успешного создания invoice для пользователя.

### `offer.html` — Публичная оферта

---

## Mobile API (`mobile_api`) — авторизация мобильного приложения

Django-приложение `mobile_api` обслуживает мобильное приложение Monkey Island
(на базе hiddify/sing-box). Авторизация — по одноразовому device-code, как у
существующего web-login через бота: бот по `/start app_<code>` сохраняет
`hash(code) → user`, приложение меняет код на access-токен.

**Эндпоинты** (`web_app/urls.py`):

| Метод/путь | Назначение |
|---|---|
| `POST /api/mobile/v1/auth/exchange` `{code}` | Обменять одноразовый код на `{access_token, subscription_url, user}` (код one-time, TTL 10 мин) |
| `GET /api/mobile/v1/me` (Bearer) | Статус подписки: `status`, `expire_at`, `days_left`, `subscription_url` |
| `GET /api/mobile/v1/tariffs` | Список тарифов |
| `POST /api/mobile/v1/auth/logout` (Bearer) | Отозвать access-токен |
| `GET /app/auth-redirect?code=…` | Мост https → `monkeyisland://auth?code=…` (кнопка «Войти» в боте) |

Авторизация API — заголовок `Authorization: Bearer <access_token>`. В БД хранятся
только хэши (`sha256` с `SECRET_KEY`, как у telegram-login). Эндпоинты `csrf_exempt`
(токен-авторизация, не сессии).

**Новые таблицы БД** (в общем сабмодуле `common/models/db.py`, аддитивно — существующие
не меняются): `mobile_auth_codes` (code_hash, user_id, expires_at, used_at, source),
`mobile_access_tokens` (token_hash, user_id, revoked_at, last_seen_at). Так как `common` —
общий сабмодуль, изменение видно и боту.

> ⚠️ **Требуется миграция.** Сгенерировать штатным скриптом (не вручную):
> `./common/alembic-revision.sh "add mobile auth tables"`, затем применить.

Тесты: `python manage.py test mobile_api` (логика кодов/токенов на in-memory SQLite).

---

## Админка: runtime-настройки

Раздел настроек в `admin_dashboard.html` управляет таблицей `system_settings`
через `support_admin_api_runtime_settings` (`engine/views.py`). Список и валидация
полностью data-driven из `common/models/settings.py` (`RUNTIME_SETTING_KEYS` +
типовые множества `INT_/POSITIVE_INT_/NON_NEGATIVE_INT_RUNTIME_SETTINGS` и т.д.):
любой ключ, добавленный в common, автоматически появляется в админке. Пустое
значение = fallback на env/default; «Сбросить» удаляет строку из БД.

Группировка в UI задаётся константой `SETTING_GROUPS` (шаблон). Ключи, не попавшие
ни в одну группу, всё равно показываются отдельным блоком в конце.

### Win-back настройки

Группа **Win-back** конфигурирует кампанию возврата истёкших пользователей
(сервисы user-notify/bot/payment/email):

- `winback_price_month`, `winback_price_threemonths`, `winback_price_year` — сниженные
  цены тарифов в win-back витрине (на витрине «все тарифы» месяц показывается по
  регулярной цене, скидка — только на 3 и 12 мес.).
- `winback_send_hour_start`, `winback_send_hour_end` — окно отправки (часы МСК).
- `winback_offer_ttl_hours` — срок жизни персонального оффера (`winback_offers`).
- `winback_grace_hours` — грейс после истечения оффера, в течение которого скидку
  ещё разово отдают.

Чтобы группа отобразилась, в `common` сайта должны присутствовать win-back ключи
из `common/models/settings.py` — пропагируйте common, как и для остальных сервисов.

---

## Деплой

Смотри `docker/website/PRODUCTION.md` и `docker/edge/PRODUCTION.md`.

Конфиги: `docker/website/docker-compose.yml` + `nginx.conf.template`.
