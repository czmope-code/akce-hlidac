import os
import re

os.environ.update(DATABASE_URL="sqlite:///:memory:", ADMIN_PASSWORD="pw", COOKIE_SECURE="false",
                  SCRAPE_ENABLED="false")

from datetime import timedelta  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app import db, jobs, scrapers  # noqa: E402

db.engine.dispose()
from sqlalchemy import create_engine  # noqa: E402

db.engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
db.SessionLocal.configure(bind=db.engine)

from app import main  # noqa: E402
from app.models import FoundOffer, TrackedItem, utcnow  # noqa: E402

main.engine = db.engine


def login(c):
    r = c.post("/login", data={"username": "admin", "password": "pw"}, follow_redirects=False)
    assert r.status_code == 303


def csrf(c, path="/items"):
    return re.search(r'name="csrf" value="([^"]+)"', c.get(path).text).group(1)


def test_auth_required_and_crud():
    with TestClient(main.app) as c:
        assert c.get("/", follow_redirects=False).headers["location"] == "/login"
        login(c)
        t = csrf(c)
        assert c.post("/items", data={"name": "máslo"}).status_code == 403
        c.post("/items", data={"name": "máslo", "csrf": t})
        with db.SessionLocal() as s:
            item = s.query(TrackedItem).one()
        c.post(f"/items/{item.id}/toggle", data={"csrf": t})
        c.post(f"/items/{item.id}/edit", data={"name": "mléko", "csrf": t})
        with db.SessionLocal() as s:
            item = s.query(TrackedItem).one()
            assert item.name == "mléko" and not item.enabled
        c.post(f"/items/{item.id}/delete", data={"csrf": t})
        with db.SessionLocal() as s:
            assert s.query(TrackedItem).count() == 0


def test_bad_login():
    with TestClient(main.app) as c:
        r = c.post("/login", data={"username": "admin", "password": "x"})
        assert "Neplatné" in r.text


def test_cleanup():
    with db.SessionLocal() as s:
        it = TrackedItem(name="a")
        s.add(it)
        s.flush()
        s.add(FoundOffer(item_id=it.id, source="kupi.cz", product_name="old",
                         discovered_at=utcnow() - timedelta(days=31)))
        s.add(FoundOffer(item_id=it.id, source="kupi.cz", product_name="new"))
        s.commit()
    assert jobs.cleanup_old() >= 1
    with db.SessionLocal() as s:
        assert [o.product_name for o in s.query(FoundOffer)] == ["new"]


def test_parsers():
    assert scrapers.parse_price("Máslo 39,90 Kč") == 39.9
    from datetime import date
    assert scrapers.parse_dates("platí 1.10. - 7.10.2026") == (date(2026, 10, 1), date(2026, 10, 7))
    html = '<div class="product"><h3><a href="/x">Máslo</a></h3><span class="shop">Albert</span> 39,90 Kč 1.10. - 7.10.</div>'
    o = scrapers.parse_kupi(html)
    assert len(o) == 1 and o[0].store == "Albert" and o[0].url == "https://www.kupi.cz/x"


def test_run_searches_skips_disabled_and_dedups(monkeypatch):
    with db.SessionLocal() as s:
        s.query(FoundOffer).delete()
        s.query(TrackedItem).delete()
        s.add_all([TrackedItem(name="on"), TrackedItem(name="off", enabled=False)])
        s.commit()
    monkeypatch.setattr(scrapers, "search", lambda c, src, q: [scrapers.Offer(src, q, "Albert", 9.9)])
    assert jobs.run_searches(client=object()) == 2
    assert jobs.run_searches(client=object()) == 0
    with db.SessionLocal() as s:
        assert {o.product_name for o in s.query(FoundOffer)} == {"on"}
