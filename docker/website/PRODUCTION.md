# Продакшн-деплой: раздельные `website` и `postgres`

## Структура на сервере

На сервере ожидается такая структура:

```text
/root/website/
  website/
    .env
    docker-compose.yml
    nginx.conf
    monkey-island-website-amd64.tar
    letsencrypt/
    certbot-work/
    certbot-logs/
    certbot-www/
    cloudflare-ips/
  postgres/
    .env
    docker-compose.yml
```

`website` и `postgres` запускаются отдельными `docker compose`, но находятся в одной внешней Docker-сети `monkey-island-network`.

## Что где лежит в репозитории

- `docker/website/Dockerfile` - образ Django-приложения;
- `docker/website/entrypoint.sh` - миграции, `collectstatic`, старт `gunicorn`;
- `docker/website/docker-compose.yml` - стек сайта;
- `docker/website/nginx.conf` - reverse proxy и TLS;
- `docker/website/deploy.sh` - деплой сайта;
- `docker/postgres/docker-compose.yml` - стек PostgreSQL;
- `docker/postgres/deploy.sh` - деплой PostgreSQL.

## Как связаны `website` и `postgres`

- `postgres` поднимается в отдельном compose-проекте;
- сервис `postgres` подключен к внешней сети `monkey-island-network`;
- контейнер `app` из `website` подключен к той же сети;
- Django ходит в базу по имени хоста `postgres`.

Поэтому `WEB_DATABASE_URL` должен выглядеть так:

```env
WEB_DATABASE_URL=postgresql://web_user:replace-me@postgres:5432/web_db
```

## Переменные для `/root/website/postgres/.env`

Минимум:

```env
POSTGRES_DB=web_db
POSTGRES_USER=web_user
POSTGRES_PASSWORD=replace-me
```

Если `SERVICE_DATABASE_URL` тоже должен указывать в этот же PostgreSQL, используй отдельную базу или отдельного пользователя по своей схеме.

## Переменные для `/root/website/website/.env`

Минимально:

```env
SECRET_KEY=replace-me
DEBUG=False

ALLOWED_HOSTS=monkeyislandvpn.com,monkey-island-vpn.com,monkey-island-vps.com,mnk-island.org
CSRF_TRUSTED_ORIGINS=https://monkeyislandvpn.com,https://monkey-island-vpn.com,https://monkey-island-vps.com,https://mnk-island.org

PROMO_DOMAINS=monkeyislandvpn.com,monkey-island-vpn.com
NEUTRAL_DOMAINS=monkey-island-vps.com
CABINET_DOMAINS=mnk-island.org
DEFAULT_CABINET_DOMAIN=mnk-island.org

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

LETSENCRYPT_EMAIL=admin@example.com
CF_DNS_API_TOKEN=replace-me
CERTBOT_CERT_NAME=monkey-island-sites
CERTBOT_DOMAINS=monkey-island-vps.com,monkeyislandvpn.com,monkey-island-vpn.com,mnk-island.org
CF_DNS_PROPAGATION_SECONDS=30
```

Для PWA при необходимости:

```env
PWA_MIRROR_SOURCE_URL=https://raw.githubusercontent.com/your-org/your-repo/main/mirror.txt
```

Для одного сертификата на несколько зон нужен один `CF_DNS_API_TOKEN` с правами:

- `Zone / DNS / Edit`
- `Zone / Zone / Read`

## Порядок запуска

1. Сначала положить `/root/website/postgres/.env`.
2. Запустить деплой PostgreSQL:

```bash
cd docker/postgres
./deploy.sh
```

3. Потом положить `/root/website/website/.env`.
4. Запустить деплой сайта:

```bash
cd docker/website
./deploy.sh
```

Оба deploy-скрипта сами создают внешнюю сеть `monkey-island-network`, если ее еще нет.

## Что делает deploy сайта

`docker/website/deploy.sh`:

- локально собирает `amd64` tar-образ;
- на сервере ротирует старый tar в `.bak`;
- загружает runtime-файлы в `/root/website/website`;
- обновляет Cloudflare allowlist для nginx;
- выпускает сертификат через `certbot/dns-cloudflare`;
- ставит cron на auto-renew;
- делает `docker load`;
- запускает `docker compose -f docker-compose.yml up -d --no-build`.

## Сертификаты и Cloudflare

Сайт рассчитан на работу за Cloudflare CDN:

- nginx принимает запросы только от Cloudflare IP ranges;
- реальный IP клиента берется из `CF-Connecting-IP`;
- сертификаты выпускаются через DNS-01 challenge;
- renewal выполняется по cron.

Рекомендуемые настройки в Cloudflare:

- все боевые DNS-записи должны быть `Proxied`;
- SSL mode: `Full (strict)`.

## Полезные команды

Сайт:

```bash
cd /root/website/website
docker compose -f docker-compose.yml ps
docker compose -f docker-compose.yml logs -f app
docker compose -f docker-compose.yml logs -f nginx
```

База:

```bash
cd /root/website/postgres
docker compose -f docker-compose.yml ps
docker compose -f docker-compose.yml logs -f postgres
```

Ручной renew сертификатов:

```bash
cd /root/website/website
./renew-certs.sh
```

## Что проверить перед выкладкой

1. что в `/root/website/postgres/.env` заполнены `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`;
2. что в `/root/website/website/.env` `WEB_DATABASE_URL` указывает на `postgres:5432`;
3. что `WEB_DATABASE_SSL_REQUIRE=False`, если используется локальный self-hosted Postgres без TLS;
4. что все домены добавлены в `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `PROMO_DOMAINS`, `NEUTRAL_DOMAINS`, `CABINET_DOMAINS`;
5. что `CF_DNS_API_TOKEN` имеет доступ ко всем четырем зонам;
6. что все нужные DNS-записи в Cloudflare включены как `Proxied`.
