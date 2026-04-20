# Production Deploy: `nginx + gunicorn`

## What this setup does

- `app` runs Django via `gunicorn`
- `nginx` accepts public HTTP traffic
- `nginx` redirects `80 -> 443`
- `nginx` proxies dynamic requests to `gunicorn`
- `nginx` serves `/static/` directly from a shared Docker volume
- `nginx` terminates TLS using mounted certificates

Your domain routing logic still lives in Django and depends on `Host`.
`nginx` forwards the original `Host` header, so `promo / neutral / cabinet`
domains keep working as expected.

## Files

- `docker/Dockerfile` - application image
- `docker/entrypoint.sh` - migrations, `collectstatic`, gunicorn startup
- `docker/nginx.conf` - reverse proxy + static files
- `docker/docker-compose.prod.yml` - production stack

## TLS certificate files

Place your certificate files into:

```text
docker/certs/fullchain.pem
docker/certs/privkey.pem
```

The nginx container mounts them as:

```text
/etc/nginx/certs/fullchain.pem
/etc/nginx/certs/privkey.pem
```

For ACME HTTP-01 challenges, nginx also exposes:

```text
docker/certbot-www/
```

through:

```text
/.well-known/acme-challenge/
```

## Required `.env`

At minimum:

```env
DEBUG=False
SECRET_KEY=replace-me

ALLOWED_HOSTS=promo.example.com,neutral.example.com,cabinet.example.com
CSRF_TRUSTED_ORIGINS=https://promo.example.com,https://neutral.example.com,https://cabinet.example.com

PROMO_DOMAINS=promo.example.com
NEUTRAL_DOMAINS=neutral.example.com
CABINET_DOMAINS=cabinet.example.com,cabinet2.example.com
DEFAULT_CABINET_DOMAIN=cabinet.example.com

SERVICE_DATABASE_URL=postgresql://user:password@db-host:5432/dbname
DATABASE_URL=sqlite:////app/db.sqlite3
```

Email example with Resend:

```env
EMAIL_PROVIDER=resend
RESEND_API_KEY=re_xxxxxxxxx
RESEND_FROM_EMAIL=Monkey Island <login@mail.example.com>
```

Optional gunicorn tuning:

```env
GUNICORN_WORKERS=3
GUNICORN_TIMEOUT=60
```

## Start

From repo root:

```bash
docker compose -f docker/docker-compose.prod.yml up --build -d
```

## Check

```bash
docker compose -f docker/docker-compose.prod.yml ps
docker compose -f docker/docker-compose.prod.yml logs -f app
docker compose -f docker/docker-compose.prod.yml logs -f nginx
```

## TLS

This setup now serves HTTPS directly from nginx on port `443`.

Options for certificates:

- Let's Encrypt / certbot on the same host
- Cloudflare Origin Certificate
- any other PEM certificate pair

If you use certbot outside Docker, point its webroot to:

```text
docker/certbot-www/
```

and then place or symlink the resulting certs into:

```text
docker/certs/fullchain.pem
docker/certs/privkey.pem
```

After certificate renewal, reload nginx:

```bash
docker compose -f docker/docker-compose.prod.yml exec nginx nginx -s reload
```
