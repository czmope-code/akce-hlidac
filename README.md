# akce-hlidac
Dockerized web app that tracks shopping list items and searches kupi.cz and akcniceny.cz for deals twice daily.

## Deployment (Docker Compose / Portainer)

The web image is built from this repo (no registry image needed):

    docker compose up -d --build

The app listens on port 8050 (`/health` for status). Postgres data lives in the named volume `pgdata`.
Override `DB_PASSWORD`, `SECRET_KEY`, `ADMIN_USER`, `ADMIN_PASSWORD` via environment variables
(Portainer stack env); defaults are test values and should be changed.
