# Edge Proxy Setup

Этот каталог предназначен для edge-серверов, которые принимают публичный трафик и проксируют его на origin с Django.

## Что здесь есть

- `docker-compose.yml` - стек edge nginx
- `deploy.sh` - деплой edge-конфига на удаленный сервер
- `nginx-promo.conf.template` - профиль для VPN/promo доменов
- `nginx-neutral.conf.template` - профиль для нейтрального лендинга
- `nginx-cabinet.conf.template` - профиль для кабинета
- `issue-certs.sh` - выпуск сертификатов через Cloudflare DNS-01
- `renew-certs.sh` - renew сертификатов и reload nginx
- `install-renew-cron.sh` - установка cron на авто-renew
- `.env.example` - пример переменных окружения

## Логика

Каждый edge-сервер:

- принимает `80/443`
- завершает TLS на себе
- проксирует запросы на origin
- сохраняет исходный `Host`, чтобы Django сам понял, это `promo`, `neutral` или `cabinet`

## Пример `.env`

```env
EDGE_SERVER_NAMES=monkey-island-vpn.com www.monkey-island-vpn.com
EDGE_CERT_NAME=monkey-island-vpn.com
EDGE_ORIGIN_UPSTREAM=http://ORIGIN_IP_OR_DOMAIN

LETSENCRYPT_EMAIL=admin@example.com
CF_DNS_API_TOKEN=replace-me
EDGE_CERTBOT_DOMAINS=monkey-island-vpn.com,www.monkey-island-vpn.com
CF_DNS_PROPAGATION_SECONDS=30
```

Рекомендуется:

- для `promo` использовать отдельный edge/IP;
- для `neutral` использовать отдельный edge/IP;
- для каждого домена кабинета использовать отдельный edge/IP, если хочешь максимальное разведение.

## Деплой

Примеры:

```bash
cd docker/edge
SSH_HOST=edge-promo ./deploy.sh --profile promo
SSH_HOST=edge-neutral ./deploy.sh --profile neutral
SSH_HOST=edge-cabinet-1 ./deploy.sh --profile cabinet
```

## Что нужно сделать на сервере заранее

1. Создать `${REMOTE_DIR:-/root/edge}/.env` по образцу `.env.example`
2. Убедиться, что домены указывают на IP edge-сервера
3. Убедиться, что Cloudflare DNS token имеет права:
   - `Zone / DNS / Edit`
   - `Zone / Zone / Read`

## Что нужно сделать на origin

Origin должен принимать трафик только от edge IP.

Минимум:

- открыть `80/443` на origin только для IP edge-серверов;
- закрыть `80/443` для всех остальных.

## Как выбирать профиль

- `promo` - для VPN/агрессивных лендингов
- `neutral` - для нейтральных страниц
- `cabinet` - для личного кабинета

Профили отличаются в основном заголовками безопасности и поведением прокси. Все они сохраняют `Host` и `X-Forwarded-*`.
