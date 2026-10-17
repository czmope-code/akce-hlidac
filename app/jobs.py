import logging
import threading
import time
from datetime import timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import delete, select

from . import config, scrapers
from .db import SessionLocal
from .models import FoundOffer, TrackedItem, utcnow

log = logging.getLogger(__name__)
_run_lock = threading.Lock()


def run_searches(client=None) -> int:
    """Search all enabled items on all sources and store results."""
    if not _run_lock.acquire(blocking=False):
        log.info("Search already running, skipping")
        return 0
    try:
        return _run_searches(client)
    finally:
        _run_lock.release()


def _run_searches(client) -> int:
    own = client is None
    client = client or scrapers.make_client()
    saved = 0
    try:
        with SessionLocal() as db:
            items = db.scalars(select(TrackedItem).where(TrackedItem.enabled)).all()
            for item in items:
                for source in scrapers.SOURCES:
                    existing = {
                        (r.product_name, r.store, r.url, float(r.price) if r.price is not None else None)
                        for r in db.scalars(select(FoundOffer).where(
                            FoundOffer.item_id == item.id, FoundOffer.source == source,
                            FoundOffer.discovered_at > utcnow() - timedelta(hours=20)))
                    }
                    for o in scrapers.search(client, source, item.name):
                        if (o.product_name, o.store, o.url, o.price) in existing:
                            continue
                        db.add(
                            FoundOffer(
                                item_id=item.id, source=o.source, product_name=o.product_name,
                                store=o.store, price=o.price, valid_from=o.valid_from,
                                valid_to=o.valid_to, url=o.url,
                            )
                        )
                        saved += 1
                    db.commit()
                    if own:
                        time.sleep(config.REQUEST_DELAY)
    finally:
        if own:
            client.close()
    log.info("Search finished, %d offers saved", saved)
    return saved


def cleanup_old(days: int | None = None) -> int:
    cutoff = utcnow() - timedelta(days=days if days is not None else config.RETENTION_DAYS)
    with SessionLocal() as db:
        res = db.execute(delete(FoundOffer).where(FoundOffer.discovered_at < cutoff))
        db.commit()
        return res.rowcount or 0


def start_scheduler() -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone=config.TIMEZONE)
    sched.add_job(run_searches, "cron", hour=config.SCRAPE_HOURS, minute=0,
                  id="search", max_instances=1, coalesce=True)
    sched.add_job(cleanup_old, "cron", hour=3, minute=30, id="cleanup")
    sched.start()
    return sched
