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


KUPI_HTML = """<html><body><div class="group_discounts">
<div class="discount_row"><a class="product_link_history" href="/slevy/vepro-krkovice">Vepřová krkovice bez kosti</a>
<span class="discount_shop"><img alt="Tesco" src="t.png"></span>
<strong class="discount_price_value">149,90 Kč</strong>
<span class="discount_validity">14. 10. - 20. 10.</span></div>
<div class="discount_row"><span class="discount_product_name">Bez ceny</span></div>
</div></body></html>"""

AKCNICENY_JSONLD = """<html><head><script type="application/ld+json">
{"@type":"Product","name":"Krkovice výběr","url":"/produkt/krkovice",
 "offers":{"@type":"Offer","price":"139.90","seller":{"name":"Albert"},
 "validFrom":"2026-10-14","priceValidUntil":"2026-10-20"}}</script></head></html>"""

AKCNICENY_HTML = """<div class="product-box"><h3><a href="/p/krkovice">Krkovice</a></h3>
<span class="shop-name">Lidl</span><span class="price">129,00 Kč</span>
<span class="validity">15.10. - 21.10.2026</span></div>"""


def test_parse_kupi_cards():
    from datetime import date
    o = scrapers.parse_kupi(KUPI_HTML, today=date(2026, 10, 15))
    assert len(o) == 1
    assert (o[0].product_name, o[0].store, o[0].price) == ("Vepřová krkovice bez kosti", "Tesco", 149.9)
    assert o[0].valid_from == date(2026, 10, 14) and o[0].valid_to == date(2026, 10, 20)
    assert o[0].url == "https://www.kupi.cz/slevy/vepro-krkovice"


def test_parse_akcniceny_jsonld_and_cards():
    from datetime import date
    o = scrapers.parse_akcniceny(AKCNICENY_JSONLD)
    assert (o[0].product_name, o[0].store, o[0].price) == ("Krkovice výběr", "Albert", 139.9)
    assert o[0].valid_to == date(2026, 10, 20) and o[0].url == "https://www.akcniceny.cz/produkt/krkovice"
    o = scrapers.parse_akcniceny(AKCNICENY_HTML)
    assert (o[0].store, o[0].price, o[0].valid_to) == ("Lidl", 129.0, date(2026, 10, 21))


def test_parse_dates_variants():
    from datetime import date
    t = date(2026, 10, 15)
    assert scrapers.parse_dates("platí do úterý 20. 10.", t) == (None, date(2026, 10, 20))
    assert scrapers.parse_dates("od 15. 10.", t) == (date(2026, 10, 15), None)
    assert scrapers.parse_dates("15. - 21. 10.", t) == (date(2026, 10, 15), date(2026, 10, 21))
    assert scrapers.parse_dates("2026-10-15 2026-10-21") == (date(2026, 10, 15), date(2026, 10, 21))
    assert scrapers.parse_dates("29.12. - 4.1.", date(2026, 12, 30)) == (date(2026, 12, 29), date(2027, 1, 4))
    assert scrapers.parse_dates("nic", t) == (None, None)


def test_parse_kupi_dates_from_card_text_and_attrs():
    from datetime import date
    html = """<div class="discount_row"><a class="product_link_history" href="/a">Krkovice</a>
<img alt="Tesco"><span class="discount_price_value">149,90 Kč</span>
<p>Platí od 14. 10. do úterý 20. 10.</p></div>
<div class="discount_row" data-valid-from="2026-10-01" data-valid-to="2026-10-07">
<a class="product_link_history" href="/b">Máslo</a><span class="discount_price_value">39 Kč</span></div>"""
    o = scrapers.parse_kupi(html, today=date(2026, 10, 15))
    assert (o[0].valid_from, o[0].valid_to) == (date(2026, 10, 14), date(2026, 10, 20))
    assert (o[1].valid_from, o[1].valid_to) == (date(2026, 10, 1), date(2026, 10, 7))


def test_parse_akcniceny_dates_time_and_heading():
    from datetime import date
    html = """<div class="product-box"><h3><a href="/p/k">Krkovice</a></h3>
<span class="price">129,00 Kč</span><time datetime="2026-10-15"></time><time datetime="2026-10-21"></time></div>"""
    o = scrapers.parse_akcniceny(html)
    assert (o[0].valid_from, o[0].valid_to) == (date(2026, 10, 15), date(2026, 10, 21))
    html = """<h1>Akce 15.10. - 21.10.2026</h1><div class="product-box"><h3><a href="/p/k">Krkovice</a></h3>
<span class="price">129,00 Kč</span></div>"""
    o = scrapers.parse_akcniceny(html)
    assert (o[0].valid_from, o[0].valid_to) == (date(2026, 10, 15), date(2026, 10, 21))


def test_parsers_fail_gracefully(caplog):
    assert scrapers.parse_kupi("<html><title>x</title></html>") == []
    assert scrapers.parse_akcniceny("") == []
    assert "no offers parsed" in caplog.text


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


def test_items_summary_open_and_aggregates():
    from datetime import date
    today = date.today()
    with TestClient(main.app) as c:
        with db.SessionLocal() as s:
            s.query(FoundOffer).delete()
            s.query(TrackedItem).delete()
            a, b, e = TrackedItem(name="krkovice"), TrackedItem(name="mléko"), TrackedItem(name="máslo")
            s.add_all([a, b, e])
            s.flush()
            s.add_all([
                FoundOffer(item_id=a.id, source="x", product_name="drahá", store="T", price=120,
                           valid_to=today + timedelta(days=1)),
                FoundOffer(item_id=a.id, source="x", product_name="levná", store="T", price=99.9,
                           valid_to=today, url="http://u"),
                FoundOffer(item_id=a.id, source="x", product_name="stará", price=10,
                           valid_to=today - timedelta(days=1)),
                FoundOffer(item_id=b.id, source="x", product_name="bez dat", price=20),
            ])
            s.commit()
        r = c.get("/api/items/summary")
        assert r.status_code == 200
        data = r.json()
        assert data["updated_at"]
        by = {i["name"]: i for i in data["items"]}
        assert by["krkovice"]["offers_count"] == 2
        assert by["krkovice"]["best_offer"]["product_name"] == "levná"
        assert by["krkovice"]["best_offer"]["price"] == 99.9
        assert by["mléko"]["best_offer"]["valid_to"] is None
        assert by["máslo"] == {"id": e.id, "name": "máslo", "offers_count": 0, "best_offer": None}
