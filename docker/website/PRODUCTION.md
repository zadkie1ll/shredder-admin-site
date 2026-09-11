# Origin Deploy

Этот каталог теперь описывает origin-сервер с Django.

Публичные домены пользователей должны смотреть не сюда, а на edge-серверы.
Origin обслуживает только backend-трафик от edge и отвечает по техническому домену, например `origin.teaworld.uk`.

## Структура на сервере

```text
/root/website/
  website/
    .env
    docker-compose.yml
    nginx.conf.template
    monkey-island-website-amd64.tar
    letsencrypt/
    certbot-work/
    certbot-logs/
    certbot-www/
    origin-allowlist/
  postgres/
    .env
    docker-compose.yml
```

`website` и `postgres` запускаются отдельными `docker compose`, но находятся в одной внешней Docker-сети `monkey-island-network`.

## Что где лежит

- `docker/website/Dockerfile` - образ Django-приложения
- `docker/website/entrypoint.sh` - миграции, `collectstatic`, `gunicorn`
  (переменные: `GUNICORN_WORKERS` default 3, `GUNICORN_THREADS` default 4 —
  threads > 1 включает worker-class gthread, чтобы один медленный запрос
  занимал поток, а не целый процесс; `GUNICORN_TIMEOUT` default 60)
- `docker/website/docker-compose.yml` - origin стек: `app` (gunicorn), `reconciliation`
  (фоновая сверка оплат и смены email, тот же образ и `.env`) и `nginx`
- `engine/management/commands/check_common_schema.py` - проверка схемы common
  только на чтение: таблицы `website_payment_attempts`, `website_email_changes` и их колонки;
  её вызывает `deploy.sh` перед пересозданием контейнеров
- `engine/geoip_updater.py` внутри Django - автоматическая загрузка и обновление DB-IP City Lite
- `docker/website/nginx.conf.template` - backend-only nginx для origin
- `docker/website/update-origin-allowlist.sh` - генерация allowlist для edge IP
- `docker/website/issue-certs.sh` - первичный выпуск origin-сертификата по standalone HTTP-01
- `docker/website/renew-certs.sh` - renew origin-сертификата через nginx webroot без остановки сайта
- `docker/website/deploy.sh` - деплой origin

## Как это работает

- пользователь идет на edge-домен;
- edge завершает пользовательский TLS;
- edge ходит на `https://origin.teaworld.uk`;
- origin проверяет, что запрос пришел только с IP edge-серверов;
- origin передает запрос в Django;
- Django различает `promo`, `neutral`, `cabinet` по заголовку `Host`, который edge сохраняет.

## Реальный IP клиента (edge → origin → Django)

От IP клиента зависят лимиты входа (magic-link, `/support-admin/`, mobile API), аудит, строка «Ваш IP» на лендинге и адрес ноды при bootstrap.

Как IP доходит до Django:

1. edge перезаписывает `X-Forwarded-For` на `$remote_addr`, поэтому значения, подставленные клиентом, отбрасываются;
2. origin nginx дописывает IP edge (`$proxy_add_x_forwarded_for`), и Django получает `X-Forwarded-For: клиент, IP edge`;
3. непосредственный `REMOTE_ADDR` для Django — контейнер nginx из docker-подсети `monkey-island-network`;
4. Django идёт по цепочке справа налево и берёт первый адрес вне доверенных сетей.

Доверенные сети — это `TRUSTED_PROXY_NETWORKS` плюс **всегда, автоматически** `ORIGIN_ALLOWED_PROXY_CIDRS`:

- значение `TRUSTED_PROXY_NETWORKS` по умолчанию: `127.0.0.1/32,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16`. Оно покрывает docker-подсеть origin;
- публичные IP edge берутся из `ORIGIN_ALLOWED_PROXY_CIDRS`, того же списка, по которому origin nginx пускает edge. Дублировать их в `TRUSTED_PROXY_NETWORKS` не нужно.

Правила:

- Не задавайте `TRUSTED_PROXY_NETWORKS` без необходимости. Явное значение **заменяет** приватные сети по умолчанию, и docker-подсеть origin обязана в нём остаться. Иначе «клиентом» для всех станет IP контейнера nginx, и лимиты входа станут общими на весь сайт. Подсеть можно посмотреть так:
  `docker network inspect monkey-island-network --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}'`.
- В `ORIGIN_ALLOWED_PROXY_CIDRS` указывайте адрес, с которого edge подключается к origin (IPv4 и IPv6, если edge ходит по IPv6).
- `ORIGIN_ALLOWED_PROXY_CIDRS` читают и nginx (allowlist), и Django (при старте контейнера). После изменения списка пересоздайте весь стек. Проще всего повторить `deploy.sh`; вручную — `./update-origin-allowlist.sh`, затем `docker compose -f docker-compose.yml up -d --no-build --force-recreate`.
- Не включайте на origin `set_real_ip_from`/`real_ip_header`: allowlist начнёт проверять IP посетителей, и `deny all` закроет сайт для всех.
- Edge со старым шаблоном (`$proxy_add_x_forwarded_for`) тоже даёт верный IP для IPv4-клиентов: разбор справа останавливается на адресе, который дописал edge.
- Записи `TRUSTED_PROXY_NETWORKS` и `ORIGIN_ALLOWED_PROXY_CIDRS` разбирает Python `ipaddress`. Ведущие нули в октетах (`203.0.113.010/32`) nginx принимает, а Django нет: такая запись пропускается, и в логе `app` один раз появляется ERROR `invalid TRUSTED_PROXY_NETWORKS/ORIGIN_ALLOWED_PROXY_CIDRS entry '…' ignored`. Edge тогда открывает сайт, но не доверен, и все его клиенты делят IP edge.

### IPv6-клиенты и неразрешённый IP

Edge публикует `80/443` через bridge-сеть docker, а nginx внутри слушает только IPv4. Если у публичного домена есть AAAA-запись, а у хоста edge есть IPv6, docker принимает IPv6-соединения userland-прокси (`docker-proxy` на `[::]:443`) и передаёт их в контейнер по IPv4. Тогда `$remote_addr` на edge у всех IPv6-клиентов одинаковый — шлюз bridge `172.x.0.1`, адрес из доверенных сетей.

Сайт такой адрес клиентом не считает. Если IP клиента непубличный (RFC 1918, `fc00::/7`, loopback, link-local, unspecified), `unknown` или из доверенных сетей, бакеты по IP пропускаются: magic-link, покупка без входа, вход в `/support-admin/`, мобильный `/auth/exchange`. Бакеты email, общий и «аккаунт+IP» работают. Не чаще раза в 10 минут на процесс пишется WARNING `client IP unresolved (proxy chain fully trusted): IP rate limit skipped — check edge IPv6/docker-proxy`. «Ваш IP» для таких клиентов показывает `172.x.0.1`, claim установки ноды отвечает `409`. Со старым edge-шаблоном IPv6-клиент к тому же подставляет свой `X-Forwarded-For` и обходит лимиты по IP.

Проверка до деплоя:

```bash
# локально, для каждого домена из EDGE_SERVER_NAMES всех edge
dig +short AAAA <домен>
# на каждом edge
ss -ltnp 'sport = :443'
```

Если AAAA нет или на `[::]:443` не слушает `docker-proxy`, действий не нужно. Если есть и то и другое, до деплоя уберите AAAA у доменов edge либо переведите edge на host-сеть (`docker/edge/PRODUCTION.md`, «IPv6 и docker-proxy»). Пока этого нет, edge-шаблон обновляется не позже сайта.

После деплоя откройте VPS-лендинг: в «Ваш IP» должен быть ваш адрес, а не IP edge; если у доменов есть AAAA, проверьте и с IPv6-клиента (например, с мобильного интернета). До этой проверки не запускайте bootstrap нод через домен за edge, иначе в Remnawave появится нода с адресом edge.

## DNS

Технический домен должен смотреть на origin IP:

```text
origin.teaworld.uk -> A -> ORIGIN_IP
```

Этот домен нельзя использовать в публичных ссылках, шаблонах, email и редиректах.

## Переменные для `/root/website/postgres/.env`

Минимум:

```env
POSTGRES_DB=web_db
POSTGRES_USER=web_user
POSTGRES_PASSWORD=replace-me
```

## Переменные для `/root/website/website/.env`

Минимум:

```env
SECRET_KEY=replace-me
DEBUG=False

ALLOWED_HOSTS=promo.example.com,neutral.example.com,cabinet.example.com
CSRF_TRUSTED_ORIGINS=https://promo.example.com,https://neutral.example.com,https://cabinet.example.com

PROMO_DOMAINS=promo.example.com
NEUTRAL_DOMAINS=neutral.example.com
CABINET_DOMAINS=cabinet.example.com
DEFAULT_CABINET_DOMAIN=cabinet.example.com

WEB_DATABASE_URL=postgresql://web_user:replace-me@postgres:5432/web_db
WEB_DATABASE_SSL_REQUIRE=False
SERVICE_DATABASE_URL=postgresql://user:password@db-host:5432/service_db

# Рекомендуется: общие для всех gunicorn-воркеров лимиты входа и кэш отчётов.
CACHE_REDIS_URL=redis://:replace-me@redis-host:6379/2

RWMS_HOST=rwms-host
RWMS_PORT=50052

PAYMENT_GATEWAY=wata
WATA_HOST=https://wata.example.com
WATA_TOKEN=replace-me
YOOKASSA_SHOP_ID=replace-me
YOOKASSA_SECRET_KEY=replace-me

EMAIL_PROVIDER=smtp
EMAIL_HOST=smtp.example.com
EMAIL_PORT=465
EMAIL_USE_SSL=True
EMAIL_HOST_USER=login@example.com
EMAIL_HOST_PASSWORD=replace-me

TG_BOT_USERNAME=monkeyislandvpnbot
TELEGRAM_AUTH_BOT_TOKEN=replace-me
# Опционально: разные Telegram Login-боты для разных доменов.
# Формат: domain|bot_username|bot_token,domain2|bot_username2|bot_token2
TELEGRAM_AUTH_BOTS=monkey-island-vps.com|monkeyislandvpsauthbot|replace-me,monkey-island-vpn.com|monkeyislandvpnauthbot|replace-me

# DB-IP City Lite скачивается автоматически без аккаунта и ключей.
GEOIPUPDATE_INTERVAL_HOURS=168
INFRA_GEOIP_CACHE_SECONDS=60

ORIGIN_CERT_NAME=origin.teaworld.uk
ORIGIN_CERTBOT_DOMAINS=origin.teaworld.uk
ORIGIN_ALLOWED_PROXY_CIDRS=203.0.113.10/32,203.0.113.11/32
LETSENCRYPT_EMAIL=admin@example.com
```

`ORIGIN_ALLOWED_PROXY_CIDRS` — список edge IP/CIDR, которым разрешено ходить на origin по HTTPS. Этот же список Django автоматически считает доверенными прокси (см. «Реальный IP клиента»).

Для DB-IP City Lite не нужны аккаунт, ключи или ручная загрузка. Сам файл
`.mmdb` скачивать, копировать на сервер и монтировать с host FS не нужно.

Для удобства рядом с этим каталогом можно держать шаблон и собирать итоговый `.env` по нему:

```bash
cp docker/website/.env.example /root/website/website/.env
```

### Необязательные переменные: прокси, кэш, лимиты, таймауты

Все они читаются контейнерами `app` и `reconciliation` из того же `.env`. Если значение по умолчанию подходит, строку не добавляйте. Числовые переменные не оставляйте пустыми (`KEY=` без числа роняет запуск Django). У лимитов `MAGIC_LINK_*`, `PAYMENT_ANON_*` и `ADMIN_LOGIN_*` значение `0` или меньше выключает именно этот лимит.

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `TRUSTED_PROXY_NETWORKS` | `127.0.0.1/32,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16` | Доверенные прокси для `X-Forwarded-For`. К списку всегда добавляется `ORIGIN_ALLOWED_PROXY_CIDRS`. Явное значение заменяет приватные сети: docker-подсеть origin должна в нём остаться |
| `CACHE_REDIS_URL` | пусто — локальный кэш процесса | Redis для лимитов входа и кэша отчётов, общих для всех воркеров. Без него каждый gunicorn-воркер считает лимиты сам. Redis в этом compose нет: нужен внешний, доступный из `monkey-island-network` |
| `CACHE_REDIS_SOCKET_TIMEOUT_SECONDS` | `0.5` | Таймаут подключения и операции с Redis. При сбое Redis лимиты входа пропускают запрос и пишут ошибку в лог |
| `RWMS_RPC_TIMEOUT_SECONDS` | `8` | Deadline обычного gRPC-вызова RWMS с сайта: кабинет, оплата, смена email, воркер `reconciliation` и мобильный API (`mobile_api/subscription.py`). Битое значение (не число, `0` или меньше) заменяется значением по умолчанию с предупреждением в логе |
| `RWMS_BULK_RPC_TIMEOUT_SECONDS` | `30` | Deadline тяжёлых вызовов RWMS: `GetAllUsers` при legacy-скане регистрации и `CreateNode` при установке ноды. `GetNodeUsersUsage` отчёта «Трафик нод» получает этот же дедлайн, но не дольше остатка бюджета отчёта (20 с) и не меньше 1 с |
| `SITE_LEGACY_RWMS_IDENTITY_SCAN_ENABLED` | `false` | Аварийный полный скан пользователей панели при регистрации legacy-аккаунтов. Штатно выключен; если панель не ответила, регистрация откладывается («повторите позже»). Покупка с лендинга без входа при недоступной панели и выключенном флаге не блокируется (аккаунт без подписки и `ALERT: RWMS unavailable during anonymous purchase` в логе), при включённом тоже получает `503` |
| `MAGIC_LINK_IP_RATE_LIMIT` | `60` | Запросов magic-link с одного IP клиента за окно |
| `MAGIC_LINK_EMAIL_RATE_LIMIT` | `5` | Запросов magic-link на один email за окно. При превышении ответ тот же (`200`), но письмо не отправляется |
| `MAGIC_LINK_RATE_WINDOW_SECONDS` | `900` | Окно лимитов magic-link по IP и email, сек |
| `MAGIC_LINK_GLOBAL_RATE_LIMIT` | `500` | Общий потолок запросов magic-link со всех IP |
| `MAGIC_LINK_GLOBAL_RATE_WINDOW_SECONDS` | `60` | Окно общего потолка, сек |
| `PAYMENT_ANON_IP_RATE_LIMIT` | `120` | Анонимных оплат с лендинга (`/pay/` без входа) с одного IP клиента за окно. Порог мягкий: у мобильных операторов много абонентов за одним IP. Оплата из кабинета не лимитируется |
| `PAYMENT_ANON_EMAIL_RATE_LIMIT` | `10` | Анонимных оплат на один email за окно |
| `PAYMENT_ANON_RATE_WINDOW_SECONDS` | `900` | Окно лимитов анонимной оплаты по IP и email, сек |
| `PAYMENT_ANON_GLOBAL_RATE_LIMIT` | `600` | Общий потолок анонимных оплат со всех IP. При превышении любого из лимитов оплаты ответ `429` с `Retry-After` |
| `PAYMENT_ANON_GLOBAL_RATE_WINDOW_SECONDS` | `60` | Окно общего потолка анонимных оплат, сек |
| `ADMIN_LOGIN_IP_RATE_LIMIT` | `30` | Неудачных попыток входа в `/support-admin/` с одного IP за окно. Успешный вход лимиты не расходует |
| `ADMIN_LOGIN_ACCOUNT_RATE_LIMIT` | `10` | Неудачных попыток на пару «аккаунт (логин или общий пароль) + IP» за окно |
| `ADMIN_LOGIN_RATE_WINDOW_SECONDS` | `900` | Окно лимитов входа в админку, сек |
| `ADMIN_LOGIN_ACCOUNT_ALERT_LIMIT` | `200` | Порог `ALERT` в лог о неудачных попытках на один аккаунт со всех IP. Вход не блокирует |
| `REQUEST_TIMING_EXPOSE_SERVER_TIMING` | `false` | Отдавать клиентам `Server-Timing`. В проде `false`: по времени SQL в ответе magic-link можно понять, зарегистрирован ли email |
| `SUPPORT_ATTACHMENT_X_ACCEL_REDIRECT` | `true` при `DEBUG=False` | Отдача вложений поддержки через internal location nginx `/_protected_support_media/`. Нужны `nginx.conf.template` и `docker-compose.yml` этого релиза. Аварийный откат на отдачу через Django — `false` |
| `INFRA_MAINTENANCE_BUDGET_SECONDS` | `30` (1–120) | Бюджет тика обслуживания infra_worker: после него новый шаг не начинается |

## Порядок запуска

1. Положить `/root/website/postgres/.env`
2. Запустить PostgreSQL:

```bash
cd docker/postgres
./deploy.sh
```

3. Положить `/root/website/website/.env`
4. Запустить origin:

```bash
cd docker/website
./deploy.sh
```

## Порядок релиза с миграцией common (релиз 2026-09)

Сайт этого релиза использует новые таблицы common `website_payment_attempts` и `website_email_changes`. Без них не работают оплата на сайте, страница статуса оплаты и смена email, а merge аккаунтов в боте откатывается. Порядок строгий:

1. **Проверить `.env` origin, IPv6 на edge и сертификат origin.** `ORIGIN_ALLOWED_PROXY_CIDRS` содержит все edge. `TRUSTED_PROXY_NETWORKS` не задан или содержит docker-подсеть origin. `CACHE_REDIS_URL` указывает на доступный Redis. У доменов edge нет AAAA, или на edge нет `docker-proxy` на `[::]:443` (см. «IPv6-клиенты и неразрешённый IP»). Сертификат действителен: в `/root/website/website` команда `openssl x509 -checkend 86400 -noout -in letsencrypt/live/<ORIGIN_CERT_NAME>/fullchain.pem` пишет `Certificate will not expire`, иначе см. оговорку в «Что делает deploy origin».
2. **Alembic-миграция common.** Её генерирует владелец своим скриптом (`common/alembic-revision.sh`) и коммитит в common; к БД её применяет новый бот при старте (`alembic upgrade head` в `main.py`). `python manage.py migrate` в entrypoint сайта относится только к Django и эту миграцию не заменяет. Перед коммитом убедитесь, что ревизия не пустая, создаёт обе таблицы и не трогает существующие: 2026-09-01 пустая autogenerate-миграция уже уезжала на прод.
3. **Оба бота (VPN и VPS), по одному.** Первый стартовавший бот применяет миграцию: проверьте в его логе строку alembic `Running upgrade … -> <новая ревизия>` и отсутствие ошибок, затем выкатывайте второй. Если миграция упала, бот не стартует. Новый merge переносит операции сайта на выжившего пользователя и принимает только одноразовые bind-токены. Образ сайта соберите заранее (`build-image-amd64.sh`), чтобы выкатить его сразу после ботов. Пока сайт старый, кнопка «Привязать Telegram» в кабинете не работает: новый бот не принимает старые MD5-ссылки. Перед пересозданием контейнера бота проверьте его очередь RWMS-задач: в compose бота `/app/rwms-tasks` не вынесен в volume и при `--force-recreate` пропадает (порядок — в плане `docs/audits/2026-09-10/review-fixes-2026-09-11.md`).
4. **Сайт:** `./deploy.sh --skip-build` (или `./deploy.sh`). Скрипт проверит схему новым образом и без миграции прервётся, не трогая работающие контейнеры. Поднимутся `app`, `reconciliation` и `nginx`.
5. **Каждый edge nginx** (перезапись `X-Forwarded-For`, `client_max_body_size 32m`), см. `docker/edge/PRODUCTION.md`. Edge можно обновить и до сайта. Если у доменов edge есть AAAA и docker-proxy на `[::]:443`, edge обновляется не позже сайта: со старым шаблоном IPv6-клиент подставляет свой `X-Forwarded-For`.

`monkey-island-payment` в этом релизе не обновляется: его образ и указатель common остаются прежними.

Откат:

- новые таблицы и их данные не удалять, downgrade миграции не делать;
- ботов не откатывать ни отдельно, ни вместе с сайтом: образ бота до миграции не стартует (`alembic upgrade head` при старте не находит новую ревизию: `Can't locate revision identified by …`), а старый merge не знает новых FK;
- откатывается только сайт: сначала `docker compose -f docker-compose.yml rm -sf reconciliation` (в старом образе этой команды нет), затем `deploy.sh` из предыдущего коммита. До повторного выката не работает привязка Telegram из кабинета.

## Что делает deploy origin

`docker/website/deploy.sh`:

- локально собирает `amd64` tar-образ (кроме `--skip-build`);
- загружает runtime-файлы в `/root/website/website`;
- генерирует allowlist для edge IP;
- выпускает сертификат на `ORIGIN_CERTBOT_DOMAINS` через standalone HTTP-01;
- ставит cron на renew;
- делает `docker load` и ставит тег `monkey-island-website:prod`;
- **проверяет схему common новым образом** (только чтение):
  `docker compose -f docker-compose.yml run --rm --no-deps -T --entrypoint python app manage.py check_common_schema`.
  Если таблиц или колонок нет либо БД недоступна, деплой прерывается **до** пересоздания контейнеров: `app`, `reconciliation` и `nginx` продолжают работать на старом образе, а тег `:prod` возвращается на предыдущий образ. Так cron `renew-certs.sh` (`docker compose up -d nginx`) не выкатит непроверенный образ.
  Оговорка: `issue-certs.sh` выполняется раньше `docker load` и проверки схемы. Если сертификата origin нет или он истёк, скрипт после выпуска делает `docker compose up -d` уже по новому `docker-compose.yml`, а тег `:prod` ещё указывает на старый образ. Тогда `reconciliation` создаётся из старого образа, падает с `Unknown command: 'reconcile_website_operations'` и уходит в рестарт-цикл, а при непрошедшей проверке схемы так и остаётся. Данные не страдают: удалите сервис `docker compose -f docker-compose.yml rm -sf reconciliation` и повторите деплой после миграции;
- подключает к Django постоянный volume `geoipdata`, куда фоновый infra-worker скачивает DB-IP City Lite;
- пересоздает stack (`app`, `reconciliation`, `nginx`) через `docker compose -f docker-compose.yml up -d --no-build --force-recreate` без предварительного `down`.

`--dry-run` показывает план загрузки и ничего не выполняет на сервере, в том числе проверку схемы. Локальная сборка образа без `--skip-build` при этом всё равно идёт.

Проверить схему вручную (например, после применения миграции) можно той же командой в `/root/website/website`: она только читает схему и завершается кодом 1 с перечнем недостающих таблиц и колонок.

## Сервис reconciliation

`reconciliation` запускает `python manage.py reconcile_website_operations` из того же образа и с тем же `.env`, что и `app`. Он доводит до конца платёжные попытки с неизвестным исходом и синхронизирует смену email с RWMS. Лидера выбирает PostgreSQL advisory lock, поэтому второй экземпляр ничего не делает. Ручной `--once` поверх compose запускать не нужно.

В compose заданы `init: true` (tini в роли PID 1 передаёт `SIGTERM` в python) и `stop_grace_period: 40s`. По `docker compose stop` или при деплое команда завершает текущий шаг и выходит сама, без `SIGKILL` посреди запроса к провайдеру или RWMS.

Без миграции common сервис не падает и не уходит в рестарты: он приостанавливается и не чаще раза в 10 минут пишет `website reconciliation paused: схема common не готова (...)`. После применения миграции работа продолжится сама, с записью `website reconciliation resumed`.

## DB-IP City Lite: автоматическая загрузка и обновление

Отдельного сервиса обновления нет. Лидер-поток `infra_worker` внутри Django
скачивает `DBIP-City-Lite.mmdb` при первом запуске и проверяет обновление раз в
168 часов. База хранится в именованном Docker volume `geoipdata`, поэтому
переживает пересоздание контейнера `app`. Рабочий путь внутри приложения:
`/var/lib/monkey-island/geoip/DBIP-City-Lite.mmdb`.

На host не нужны `geoipupdate`, cron, отдельный каталог для `.mmdb`, `scp` или
ручное обновление. Аккаунт, Account ID и license key не нужны.

Загрузчик получает ежемесячную
[DB-IP City Lite](https://db-ip.com/db/download/ip-to-city-lite) в
[MMDB-формате](https://db-ip.com/db/format/ip-to-city-lite/mmdb.html): сначала
пробует выпуск текущего месяца, а при HTTP 404 — предыдущего. После загрузки
gzip распаковывается, MMDB валидируется и атомарно заменяет рабочий файл. Если
DB-IP временно недоступен или файл повреждён, origin продолжит работать без
географической аналитики либо со старой рабочей базой. Повтор после ошибки
выполняется не чаще раза в час.
Проверка после деплоя:

```bash
cd /root/website/website
docker compose -f docker-compose.yml logs --tail=200 app | grep -i DB-IP
docker compose -f docker-compose.yml exec app python manage.py shell -c \
  "from engine.geoip_lookup import configured_database; print(configured_database())"
```

Последняя команда должна вывести объект `GeoIpDatabase`, а не `None`.

## Origin сертификат

Origin использует отдельный сертификат на технический домен, например `origin.teaworld.uk`.

Пользовательские домены не должны входить в этот сертификат.

Первичный выпуск сертификата использует standalone HTTP-01 и может кратко остановить только nginx, если origin уже был запущен. Если выпуск завершится ошибкой, скрипт вернет origin stack в прежнее состояние.

Плановый renew использует webroot (`/var/www/certbot`) через уже работающий nginx и не должен выполнять `docker compose down`. Это важно: `docker compose down` удаляет контейнеры и обычные compose-логи, а при ошибке certbot может оставить сайт выключенным.

Edge должен ходить на origin так:

```env
EDGE_ORIGIN_UPSTREAM=https://origin.teaworld.uk
EDGE_ORIGIN_TLS_NAME=origin.teaworld.uk
EDGE_ORIGIN_TLS_VERIFY=on
```

## Ограничение доступа

Origin должен принимать запросы только от edge.

Защита в два слоя:

1. `nginx` allowlist через `ORIGIN_ALLOWED_PROXY_CIDRS` (тот же список Django использует как доверенные прокси)
2. firewall на сервере origin

Примерно так:

```bash
ufw allow 80/tcp
ufw allow from EDGE_IP_1 to any port 443
ufw allow from EDGE_IP_2 to any port 443
ufw deny 443
```

Порт `80` нужен для HTTP-01 challenge certbot и должен быть доступен снаружи во время первичного выпуска и renew. В обычном режиме nginx отдает на 80 порту только `/.well-known/acme-challenge/`, остальные запросы закрывает.
Порт `443` можно и нужно ограничивать только edge IP.

## Размер тела запроса

`client_max_body_size 32m` задан и в origin nginx, и на edge (`docker/edge/nginx.conf.template`); значения должны совпадать. Приложение само ограничивает вложения поддержки: до трёх файлов, не более 10 MiB каждый и 25 MiB суммарно. Если edge не обновлён, крупные вложения получат `413` уже на edge.

## Полезные команды

```bash
cd /root/website/website
docker compose -f docker-compose.yml ps
docker compose -f docker-compose.yml logs -f app
docker compose -f docker-compose.yml logs -f reconciliation
docker compose -f docker-compose.yml logs -f nginx
docker compose -f docker-compose.yml run --rm --no-deps -T --entrypoint python app manage.py check_common_schema
./renew-certs.sh
./update-origin-allowlist.sh
```

Если origin внезапно оказался остановлен, сначала проверьте cron-лог renew:

```bash
tail -n 200 /var/log/monkey-island-origin-renew.log
docker compose -f docker-compose.yml ps -a
```

## Что проверить

1. что `origin.teaworld.uk` смотрит на origin IP;
2. что `ORIGIN_CERT_NAME` и `ORIGIN_CERTBOT_DOMAINS` совпадают с техническим доменом;
3. что `ORIGIN_ALLOWED_PROXY_CIDRS` содержит все edge IP;
4. что пользовательские домены смотрят только на edge, а не на origin;
5. что `WEB_DATABASE_SSL_REQUIRE=False` для локального self-hosted Postgres без TLS;
6. что в логах `app` нет ошибок DB-IP, а Django видит `DBIP-City-Lite.mmdb`;
7. что `TRUSTED_PROXY_NETWORKS` не задан или содержит docker-подсеть origin, а на VPS-лендинге «Ваш IP» показывает адрес клиента, а не edge;
8. что `CACHE_REDIS_URL` задан и в логах `app` нет ошибок `rate limit cache failed`;
9. что `check_common_schema` завершается успешно, а в логах `reconciliation` нет строки `website reconciliation paused: схема common не готова`;
10. что вложения поддержки открываются (internal location `/_protected_support_media/` и volume `mediafiles` в nginx);
11. что в логах `app` нет `client IP unresolved (proxy chain fully trusted)` и `invalid TRUSTED_PROXY_NETWORKS/ORIGIN_ALLOWED_PROXY_CIDRS entry` (иначе см. «Реальный IP клиента»);
12. что на строки `ALERT: RWMS unavailable during anonymous purchase` (покупка с лендинга при недоступной панели) и `checkout needs reconciliation` настроены алерты.
