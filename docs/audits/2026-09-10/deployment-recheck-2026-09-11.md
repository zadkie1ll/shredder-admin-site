# Повторная проверка и порядок деплоя — 11 сентября 2026

> **Устарело.** Актуальные итоги ревью, исправления и план деплоя —
> [review-fixes-2026-09-11.md](review-fixes-2026-09-11.md). Ниже исправлены только
> прямые противоречия: payment в релиз не входит (правка откатана), лимиты и IP
> клиента живут в `engine/`, миграцию применяет стартующий бот, откатывается только сайт.

Проверена локальная рабочая копия. Деплой, миграции, запросы к production БД,
оплаты и операции Remnawave не выполнялись. Изменения пока не закоммичены;
есть новые untracked файлы, которые нужно включить в релиз, в том числе assets,
worker, ORM helpers, тесты и package-lock.json.

## Что проверено заново

- 617 изолированных Python-тестов website — OK; 51 класс fixtures БД исключён,
  SQLAlchemy connect и живой HTTP запрещены в этом прогоне.
- 5 JS-тестов — OK; 3 теста payment — OK; 5 тестов merge/FK бота — OK.
- Django system check — OK; collectstatic с ManifestStorage во временный каталог:
  210 entries, 8 выделенных assets — OK.
- git diff --check — OK. Новые две ORM-модели совпадают в трёх копиях common.
- Проверены фактические deploy/build/compose/entrypoint скрипты. Минимальная
  Docker-сборка без сети подтвердила корректный путь site-packages Python 3.12
  из Dockerfile бота/payment; это не свежая полная сборка этих приложений.

Новых воспроизведённых ошибок приложения в этом прогоне не найдено. Полноценная
проверка конкурентных транзакций PostgreSQL, live callbacks и свежих production
образов остаётся обязательной частью проверки релиза. Ранее проверенные браузер
и nginx в этой итерации заново не запускались.

## Репозитории и единицы развёртывания

| Репозиторий | Изменения этой задачи | Что разворачивать |
| --- | --- | --- |
| monkey-island-website | Backend, mobile API, кабинет/админка, статика, таймауты, отчёты, worker, Docker/nginx | app + reconciliation + origin nginx; отдельно конфиг каждого edge nginx |
| monkey-island-vpn-bot | Одноразовая привязка Telegram, перенос платежных операций и очереди email при merge, тесты | Оба используемых варианта: VPN bot и VPS bot |
| monkey-island-payment | Правка (ранний Wata callback по попытке оплаты) откатана | Не разворачивать в этом релизе |
| monkey-island-common | Две ORM-модели; клиенты RWMS с timeout; `runtime_tariffs.py`; тесты (лимиты и IP перенесены в `engine/`) | Отдельного контейнера нет: включается в образы website и bot; отдельно Alembic-миграция |

В website/common и bot/common исходный HEAD f61637b; payment/common — 98a11e8
(правка откатана, указатель payment не меняется). Перед релизом нужно собрать
один commit common из website/common и закрепить его в website и bot. Копирования
только models/db.py недостаточно: website импортирует также новый runtime_tariffs.py
(rate_limit.py и request_ip.py перенесены в engine/).
При объединении не терять более свежие модели/миграции client_snis.

В рамках этой задачи не менялись rwms, user-notify, rw-cleaner, ym-stat, email,
custom-config, ip-guard, yk-recurrent и devops. Перезапускать их только ради этого
релиза не требуется. Обновление общего submodule в других сервисах позже должно
проходить их проверки; не применять массовый update --remote ко всем сервисам.
Изменённые mobile-app и video-guides в соседних папках относятся к другой работе
и не включены в этот релиз.

## Порядок

### 1. Подготовить исходники и образы

Сначала согласовать common, затем включить изменения и новые файлы в commits
common/website/bot (payment не входит). Обновить submodule pointers. Сборка из текущей dirty
копии технически возможна, но такой образ не будет воспроизводимым релизом.
Сохранить предыдущие версии образов/конфигов для отката. Команды ниже используют
хосты/пути из текущих скриптов: сверить их со своей инфраструктурой.

### 2. Применить common-миграцию штатным способом

Миграцию генерирует и применяет владелец. В common найден скрипт
`alembic-revision.sh`: он выполняет только revision --autogenerate, а не upgrade.
Проверить результат autogenerate: должны быть новые website_payment_attempts и
website_email_changes с PK, FK и индексами из ORM; эта задача не требует удаления
users, изменения существующих колонок или подписок. Учитывать предыдущие
неприменённые ревизии отдельно. К целевой БД её применяет новый бот при старте
(`alembic upgrade head`), поэтому боты выкатываются первыми и по одному.

`python manage.py migrate` в entrypoint сайта относится к Django и НЕ заменяет
эту Alembic-миграцию. PostgreSQL контейнер пересобирать/переинициализировать не нужно.

### 3. Payment не обновлять

Правка ИИ в monkey-island-payment откатана, в этот релиз payment не входит: его
образ и указатель common остаются прежними.

### 4. Обновить оба варианта бота

Локально:

```bash
cd /Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-vpn-bot/docker/vpn
bash build-image-amd64.sh
bash deploy.sh
cd /Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-vpn-bot/docker/vps
bash build-image-amd64.sh
bash deploy.sh
```

На сервере (только варианты, реально используемые в production):

```bash
ssh mi.fornex.app
cd /srv/monkey-island/vpn-bot
docker load -i monkey-island-vpn-bot-amd64.tar
docker compose up -d --no-build --force-recreate monkey-island-vpn-bot
docker compose ps
docker compose logs --tail=100 monkey-island-vpn-bot
cd /srv/monkey-island/vps-bot
docker load -i monkey-island-vps-bot-amd64.tar
docker compose up -d --no-build --force-recreate monkey-island-vps-bot
docker compose ps
docker compose logs --tail=100 monkey-island-vps-bot
```

Бот должен обновиться до появления новых операций сайта: старый merge не знает
их FK и может получить ошибку при объединении аккаунтов. Проверять merge на
контролируемых тестовых аккаунтах, не на случайно выбранных клиентах.

### 5. Обновить сайт и origin nginx

Проверить .env на сервере: app и reconciliation должны использовать одну БД,
настройки Wata/YooKassa и RWMS. CACHE_REDIS_URL нужен для общих между workers
кэша и rate limits. TRUSTED_PROXY_NETWORKS по умолчанию покрывает Docker subnet,
а IP edge Django добавляет сам из ORIGIN_ALLOWED_PROXY_CIDRS; явное значение
обязано содержать Docker subnet origin. Не доверять всему интернету.
SUPPORT_ATTACHMENT_X_ACCEL_REDIRECT=true включать вместе с новым origin nginx;
при DEBUG=false это значение по умолчанию. INFRA_MAINTENANCE_BUDGET_SECONDS=30
можно оставить по умолчанию.

Локально:

```bash
cd /Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website
bash docker/website/deploy.sh
```

Скрипт сам собирает amd64 tar, загружает файлы, обновляет сертификаты/allowlist,
загружает и тегирует образ и выполняет compose up --force-recreate для всего
стека. В отличие от payment/bot, дополнительный docker load обычно не нужен.
Стартуют app, reconciliation и nginx. Не использовать --skip-build для этого
релиза со старым архивом. Флаг --dry-run скрипта может всё равно собирать образ
и выполнять rsync dry-run по SSH; при этой проверке он не запускался.

Проверить на origin:

```bash
ssh mi.fornex.website
cd /root/website/website
docker compose ps
docker compose logs --tail=100 app reconciliation nginx
docker compose exec nginx nginx -t
```

app выполняет collectstatic при старте; reconciliation использует тот же образ
и отдельный entrypoint. Команда worker --once выполняет один цикл без ожидания;
запускать её чаще раза в 31 секунду и поверх compose не нужно.

### 6. Обновить каждый edge nginx

В репозитории website изменены client_max_body_size (32m) и перезапись XFF.
Локально для каждого действующего edge, подставляя его SSH alias:

```bash
cd /Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website
SSH_HOST=mi.edge1 bash docker/edge/deploy.sh
```

Edge deploy.sh выполняет up -d без force-recreate. Одного изменения содержимого
bind-mounted шаблона недостаточно для гарантированной перегенерации nginx config.
После загрузки на каждом edge (default remote path /root/edge):

```bash
ssh mi.edge1
cd /root/edge
docker compose up -d --no-build --force-recreate nginx
docker compose exec nginx nginx -t
docker compose ps
```

### 7. Проверить релиз и возможность отката

Проверить вход и кабинет на VPN/VPS/cabinet доменах, выбранный тариф после входа,
привязку Telegram, страницы поддержки, загрузку допустимых вложений, приватность
прямого media URL и отсутствие ошибок статики. Через тестовый/контролируемый
сценарий проверить создание заказа, callback, неизвестный исход и восстановление,
подтверждение email и RWMS sync. У worker не должно быть ошибок отсутствующей
таблицы/колонки и бесконечных restart. Состояния review требуют разбора по заказу;
они не означают, что платёж не прошёл.

При откате сохранить новые таблицы, данные и новый бот: образ бота до миграции
не стартует (alembic upgrade head не находит ревизию), старый merge не знает новых
FK. Откатывается только сайт (сначала удалить сервис reconciliation). Не выполнять
downgrade с удалением очереди операций ради отката сайта.

Оставшиеся функциональные и performance-задачи перечислены в
[отчёте об исправлениях](implementation-fixes-2026-09-11.md): полная проверка видео,
дальнейшее разделение frontend, история сообщений, точные 24h buckets ip-guard,
интеграционная и нагрузочная проверка. Весь исходный план закрытым не считается.
