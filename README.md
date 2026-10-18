# akce-hlidac

Web app tracking shopping-list items and searching deals on kupi.cz and akcniceny.cz
twice a day (default 07:00 and 17:00 Europe/Prague). Results are kept for 30 days.

Stack: FastAPI + Jinja2, PostgreSQL, APScheduler (in-app), Docker Compose.

## Deploy (Portainer)
Stacks → Add stack → Repository → this repo, compose path `docker-compose.yml`.
Set the environment variables below in Portainer.

The web service listens on **port 8050**. PostgreSQL is on an internal-only network
and has no published port. Put a TLS-terminating reverse proxy (Nginx Proxy Manager,
Traefik, Caddy) in front and point it at `web:8050` (or host port 8050). If the proxy
is on the same Docker network you can remove the `ports:` mapping. Always use HTTPS:
session cookies are `Secure` by default.

## Local run
```
cp .env.example .env   # edit values
docker compose --env-file .env up --build
```
Open http://localhost:8050 (set `COOKIE_SECURE=false` for plain HTTP).

## Environment variables
| Name | Default | Description |
|---|---|---|
| `DB_PASSWORD` | required | PostgreSQL password |
| `SECRET_KEY` | required | Session signing key (`openssl rand -hex 32`) |
| `ADMIN_USER` | `admin` | Admin login |
| `ADMIN_PASSWORD` | required | Admin password (login disabled if empty) |
| `RETENTION_DAYS` | `30` | History retention (cleanup runs daily 03:30) |
| `SCRAPE_HOURS` | `7,17` | Hours of daily searches |
| `COOKIE_SECURE` | `true` | Secure session cookie flag |
| `SCRAPER_DELAY_SECONDS` | `3` | Delay between scraper requests |

## Development
```
pip install -r requirements.txt
pytest
```
The scrapers in `app/scrapers.py` are a best-effort generic parser; improve
`parse_kupi` / `parse_akcniceny` once the real HTML structure is verified, and
check the sites' robots.txt and terms of use.
Login attempts are rate limited (5 per 5 minutes per IP). Behind a proxy, uvicorn
runs with `--proxy-headers` so the real client IP is used.

## Home Assistant API

`GET /api/items/summary` is an open, read-only JSON endpoint (no login): `updated_at` and
`items[]` (`name`, `offers[]`), where each active offer has only `item`, `description`,
`store`, `price`, `valid_from`, `valid_to`. Expired offers (`valid_to` in the past) and items
without active offers are omitted; offers are sorted cheapest first.

```yaml
rest:
  - resource: http://<SERVER_IP>:8050/api/items/summary
    scan_interval: 1800
    sensor:
      - name: "Akce krkovice"
        value_template: >
          {% set i = value_json['items'] | selectattr('name', 'eq', 'krkovice') | first %}
          {{ i.offers[0].price if i else 'none' }}
        json_attributes_path: "$"
        json_attributes:
          - items
```
