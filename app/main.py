import os
import time

import psycopg2
from fastapi import FastAPI

app = FastAPI(title="akce-hlidac")

DATABASE_URL = os.getenv("DATABASE_URL", "")
SECRET_KEY = os.getenv("SECRET_KEY", "")
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
TZ = os.getenv("TZ", "UTC")
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "30"))
SCRAPE_HOURS = [int(h) for h in os.getenv("SCRAPE_HOURS", "7,17").split(",") if h.strip()]


def connect(retries=1, delay=2):
    for attempt in range(retries):
        try:
            return psycopg2.connect(DATABASE_URL, connect_timeout=5)
        except psycopg2.Error:
            if attempt == retries - 1:
                raise
            time.sleep(delay)


@app.on_event("startup")
def init_db():
    try:
        conn = connect(retries=10)
    except psycopg2.Error:
        return
    with conn, conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS items ("
            "id SERIAL PRIMARY KEY, name TEXT NOT NULL, "
            "created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
    conn.close()


@app.get("/")
def index():
    return {"app": "akce-hlidac", "scrape_hours": SCRAPE_HOURS, "retention_days": RETENTION_DAYS, "tz": TZ}


@app.get("/health")
def health():
    try:
        conn = connect()
        with conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
        conn.close()
        return {"status": "ok", "database": "ok"}
    except psycopg2.Error:
        return {"status": "degraded", "database": "unavailable"}
