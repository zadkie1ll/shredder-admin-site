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

- принимает `80/443` (порты публикуются через bridge-сеть docker, nginx в контейнере слушает только IPv4, см. «IPv6 и docker-proxy»)
- завершает TLS на себе
- проксирует запросы на origin
- сохраняет исходный `Host`, чтобы Django сам понял, это `promo`, `neutral` или `cabinet`
- перезаписывает `X-Forwarded-For` на `$remote_addr` (как и `X-Real-IP`): цепочку, присланную клиентом, edge отбрасывает, потому что именно он граница доверия
- принимает тело запроса до `client_max_body_size 32m` — столько же, сколько origin

Для схемы с техническим origin-доменом вроде `origin.teaworld.uk`:

- пользовательские домены должны смотреть только на edge IP;
- технический домен должен смотреть только на origin IP;
- edge ходит на origin по `https://origin.teaworld.uk`;
- upstream TLS на edge проверяется отдельно от пользовательского TLS.

## Реальный IP клиента и доверенные прокси

Цепочка `X-Forwarded-For`, которую видит Django: `клиент` (записал edge) → `IP edge` (дописал origin nginx) → `REMOTE_ADDR` контейнера origin nginx. Django разбирает её справа налево и берёт первый адрес вне доверенных сетей. От этого IP зависят лимиты входа (magic-link, `/support-admin/`, mobile API), аудит, «Ваш IP» на лендинге и адрес ноды при bootstrap.

Публичный IP каждого edge должен быть в `ORIGIN_ALLOWED_PROXY_CIDRS` в `.env` origin (`/root/website/website/.env`):

- по этому списку origin nginx пускает edge (`allow ...; deny all;`);
- этот же список Django автоматически добавляет к `TRUSTED_PROXY_NETWORKS`, отдельно прописывать IP edge в `TRUSTED_PROXY_NETWORKS` не нужно.

Указывать нужно адрес, с которого edge подключается к origin: исходящий IPv4, а если edge ходит на origin по IPv6 — и IPv6. Запрос с неизвестного edge получает 403 от origin, и его адрес виден в логе origin nginx.

Записи списка разбирает Python `ipaddress`, а не только nginx. Ведущие нули в октетах (`203.0.113.010/32`) nginx принимает, Django нет: edge откроет сайт, но не будет доверен, и в логе `app` появится ERROR `invalid TRUSTED_PROXY_NETWORKS/ORIGIN_ALLOWED_PROXY_CIDRS entry '…' ignored`.

Новый edge или смена его IP:

1. добавить адрес в `ORIGIN_ALLOWED_PROXY_CIDRS` на origin;
2. передеплоить origin (`docker/website/deploy.sh`): он обновляет allowlist nginx и пересоздаёт `app`/`reconciliation`, которые читают список при старте.

Одного `update-origin-allowlist.sh` с reload nginx недостаточно: сайт через новый edge откроется, но Django не будет доверять этому edge, и все его клиенты получат один IP (IP edge) и общий лимит входа.

Не возвращайте `$proxy_add_x_forwarded_for` в edge-шаблон. Схема рассчитана на прямое подключение клиентов к edge: если перед edge поставить ещё один прокси/CDN, `$remote_addr` станет адресом этого прокси, а не клиента.

## IPv6 и docker-proxy

nginx в контейнере слушает только IPv4 (`listen 443 ssl;`), а порты публикуются через bridge-сеть (`ports: "443:443"`). Docker по умолчанию публикует порт и на `[::]`: IPv6-соединения принимает userland-прокси `docker-proxy` и передаёт в контейнер по IPv4 с адреса шлюза bridge. Тогда для nginx `$remote_addr` у всех IPv6-клиентов одинаковый — `172.x.0.1`.

Что это даёт на сайте:

- `172.x.0.1` входит в доверенные сети, поэтому Django не считает его клиентом. Лимиты по IP (magic-link, покупка без входа, вход в `/support-admin/`, мобильный `/auth/exchange`) для таких клиентов пропускаются, а в логе `app` не чаще раза в 10 минут на процесс появляется WARNING `client IP unresolved (proxy chain fully trusted): IP rate limit skipped — check edge IPv6/docker-proxy`. Бакеты email и общий действуют;
- «Ваш IP» на лендинге показывает `172.x.0.1`, claim установки ноды отвечает `409`;
- со старым шаблоном (`$proxy_add_x_forwarded_for`) слева от `172.x.0.1` остаётся значение, присланное клиентом: IPv6-клиент подставляет любой адрес и обходит лимиты по IP.

Проверка — до деплоя сайта и после любых изменений DNS:

```bash
# локально, для каждого домена из EDGE_SERVER_NAMES
dig +short AAAA <домен>
# на самом edge
ss -ltnp 'sport = :443'
```

Проблема есть, если у домена есть AAAA и в выводе `ss` на `[::]:443` слушает `docker-proxy`. Варианты:

1. убрать AAAA у доменов edge: проще всего, клиенты пойдут по IPv4;
2. перевести edge на host-сеть: `network_mode: host` вместо `ports:` в `docker-compose.yml` и `listen [::]:80 default_server;`, `listen [::]:443 ssl;` в шаблоне, чтобы nginx видел исходный IPv6;
3. включить IPv6 в сети compose edge (`enable_ipv6`, ip6tables, без userland-proxy), чтобы исходный IPv6 доходил до контейнера.

Варианты 2 и 3 — отдельная правка edge, в релиз 2026-09 она не входит. Если edge при этом начнёт ходить на origin по IPv6, его IPv6-адрес тоже должен быть в `ORIGIN_ALLOWED_PROXY_CIDRS`. Пока ни один вариант не сделан, обновляйте edge-шаблон не позже сайта.

## Размер тела запроса

`client_max_body_size 32m` задан и на edge (`docker/edge/nginx.conf.template`), и на origin (`docker/website/nginx.conf.template`); значения должны совпадать. Приложение само ограничивает вложения поддержки: до трёх файлов, не более 10 MiB каждый и 25 MiB суммарно, плюс накладные расходы multipart. Меньшее значение на edge даёт `413` при загрузке вложений.

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

`deploy.sh` выполняет `docker compose up -d` без пересоздания контейнера, а nginx рендерит `nginx.conf.template` только при старте контейнера. Поэтому после изменения шаблона (например, перезапись `X-Forwarded-For` и `client_max_body_size 32m` из релиза 2026-09) на каждом edge дополнительно:

```bash
ssh <edge>
cd /root/edge
docker compose up -d --no-build --force-recreate nginx
docker compose exec nginx nginx -t
docker compose ps
```

Edge можно обновлять до или после сайта: для IPv4-клиентов Django корректно разбирает и цепочку от старого edge-шаблона. Если у доменов edge есть AAAA и на `[::]:443` слушает `docker-proxy` (см. «IPv6 и docker-proxy»), обновляйте edge не позже сайта. Обновить нужно все действующие edge.

## Что нужно сделать на сервере заранее

1. Создать `${REMOTE_DIR:-/root/edge}/.env` по образцу `.env.example`
2. Убедиться, что домены указывают на IP edge-сервера и у них нет AAAA-записей, пока edge не принимает IPv6 без `docker-proxy` (см. «IPv6 и docker-proxy»)
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
- добавить исходящий IP этого edge в `ORIGIN_ALLOWED_PROXY_CIDRS` origin и передеплоить origin (см. «Реальный IP клиента и доверенные прокси»).

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
