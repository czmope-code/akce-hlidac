import os
import secrets


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


DATABASE_URL = _env("DATABASE_URL", "sqlite:///./akce.db")
SECRET_KEY = _env("SECRET_KEY") or secrets.token_hex(32)
ADMIN_USER = _env("ADMIN_USER", "admin")
ADMIN_PASSWORD = _env("ADMIN_PASSWORD", "")
RETENTION_DAYS = int(_env("RETENTION_DAYS", "30"))
SCRAPE_HOURS = _env("SCRAPE_HOURS", "7,17")
TIMEZONE = _env("TZ", "Europe/Prague")
COOKIE_SECURE = _env("COOKIE_SECURE", "true").lower() in ("1", "true", "yes")
SCRAPE_ENABLED = _env("SCRAPE_ENABLED", "true").lower() in ("1", "true", "yes")
USER_AGENT = _env(
    "SCRAPER_USER_AGENT", "akce-hlidac/0.1 (personal deal tracker)"
)
REQUEST_DELAY = float(_env("SCRAPER_DELAY_SECONDS", "3"))
