# Edge Proxy Setup

Этот каталог предназначен для edge-серверов, которые принимают публичный трафик и проксируют его на origin с Django.

## Что здесь есть

- `docker-compose.yml` - стек edge nginx
- `deploy.sh` - деплой edge-конфига на удаленный сервер
- `nginx.conf.template` - общий шаблон nginx для любого edge
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

Для схемы с техническим origin-доменом вроде `origin.teaworld.uk`:

- пользовательские домены должны смотреть только на edge IP;
- технический домен должен смотреть только на origin IP;
- edge ходит на origin по `https://origin.teaworld.uk`;
- upstream TLS на edge проверяется отдельно от пользовательского TLS.

## Пример `.env`

```env
EDGE_SERVER_NAMES=monkey-island-vpn.com www.monkey-island-vpn.com
EDGE_CERT_NAME=monkey-island-vpn.com
EDGE_ORIGIN_UPSTREAM=https://origin.teaworld.uk
EDGE_ORIGIN_TLS_NAME=origin.teaworld.uk
EDGE_ORIGIN_TLS_VERIFY=on
EDGE_FRAME_OPTIONS=SAMEORIGIN
EDGE_REFERRER_POLICY=strict-origin-when-cross-origin
EDGE_PROXY_BUFFERING=on

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
SSH_HOST=edge-promo ./deploy.sh
SSH_HOST=edge-neutral ./deploy.sh
SSH_HOST=edge-cabinet-1 ./deploy.sh
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
- выпустить отдельный сертификат на технический origin-домен, например `origin.teaworld.uk`;
- не использовать этот технический домен в публичных URL, редиректах и шаблонах.

## Что можно менять через `.env`

- `EDGE_SERVER_NAMES` - какие домены принимает этот edge
- `EDGE_CERT_NAME` - имя сертификата в certbot
- `EDGE_ORIGIN_UPSTREAM` - куда проксировать запросы
- `EDGE_ORIGIN_TLS_NAME` - hostname для upstream TLS-проверки
- `EDGE_ORIGIN_TLS_VERIFY` - проверять ли upstream сертификат (`on`/`off`)
- `EDGE_FRAME_OPTIONS` - значение заголовка `X-Frame-Options`
- `EDGE_REFERRER_POLICY` - значение заголовка `Referrer-Policy`
- `EDGE_PROXY_BUFFERING` - `on` или `off`

То есть один и тот же шаблон можно использовать и для `promo`, и для `neutral`, и для `cabinet`, меняя только `.env` на каждом edge-сервере.
