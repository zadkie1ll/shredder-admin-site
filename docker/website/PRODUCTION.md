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
- `docker/website/docker-compose.yml` - origin стек
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

`ORIGIN_ALLOWED_PROXY_CIDRS` — список edge IP/CIDR, которым разрешено ходить на origin по HTTPS.

Для DB-IP City Lite не нужны аккаунт, ключи или ручная загрузка. Сам файл
`.mmdb` скачивать, копировать на сервер и монтировать с host FS не нужно.

Для удобства рядом с этим каталогом можно держать шаблон и собирать итоговый `.env` по нему:

```bash
cp docker/website/.env.example /root/website/website/.env
```

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

## Что делает deploy origin

`docker/website/deploy.sh`:

- локально собирает `amd64` tar-образ;
- загружает runtime-файлы в `/root/website/website`;
- генерирует allowlist для edge IP;
- выпускает сертификат на `ORIGIN_CERTBOT_DOMAINS` через standalone HTTP-01;
- ставит cron на renew;
- делает `docker load`;
- подключает к Django постоянный volume `geoipdata`, куда фоновый infra-worker скачивает DB-IP City Lite;
- пересоздает stack через `docker compose -f docker-compose.yml up -d --no-build --force-recreate` без предварительного `down`.

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

1. `nginx` allowlist через `ORIGIN_ALLOWED_PROXY_CIDRS`
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

## Полезные команды

```bash
cd /root/website/website
docker compose -f docker-compose.yml ps
docker compose -f docker-compose.yml logs -f app
docker compose -f docker-compose.yml logs -f nginx
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
6. что в логах `app` нет ошибок DB-IP, а Django видит `DBIP-City-Lite.mmdb`.
