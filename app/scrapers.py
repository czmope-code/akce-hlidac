"""Scrapers for kupi.cz and akcniceny.cz.

Parsing is best-effort and deliberately defensive: the HTML structure of the
sites may change, so each parser returns an empty list on failure. Extend
``parse_*`` functions to improve accuracy.
"""
import json
import logging
import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import quote_plus, urljoin

import httpx
from bs4 import BeautifulSoup

from . import config

log = logging.getLogger(__name__)


@dataclass
class Offer:
    source: str
    product_name: str
    store: str | None = None
    price: float | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    url: str | None = None


SOURCES = {
    "kupi.cz": "https://www.kupi.cz/hledej?f={q}",
    "akcniceny.cz": "https://www.akcniceny.cz/hledej?s={q}",
}

_PRICE_RE = re.compile(r"(\d+(?:[ \u00a0]?\d{3})*(?:[.,]\d{1,2})?)\s*(?:Kč|,-)")
_DATE_RE = re.compile(r"(\d{1,2})\.\s*(\d{1,2})\.(?:\s*(\d{4}))?")


def parse_price(text: str) -> float | None:
    m = _PRICE_RE.search(text)
    if not m:
        return None
    raw = m.group(1).replace(" ", "").replace("\u00a0", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


_ISO_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_SHARED_RANGE_RE = re.compile(
    r"(\d{1,2})\.\s*(?:(\d{1,2})\.)?\s*(?:-|–|—|až)\s*(\d{1,2})\.\s*(\d{1,2})\.(?:\s*(\d{4}))?"
)
_FROM_RE = re.compile(r"\b(?:od|platí od|from)\s*(?:[^\W\d_]+\s+)?$", re.I)


def _mk(y, mo, d) -> date | None:
    try:
        return date(int(y), int(mo), int(d))
    except ValueError:
        return None


def parse_dates(text: str, today: date | None = None) -> tuple[date | None, date | None]:
    """Extract (valid_from, valid_to) from Czech text or ISO dates.

    Handles ``15.10. - 21.10.``, ``15. - 21. 10. 2026``, ``platí do úterý 20. 10.``,
    ``od 15. 10.`` and ISO ``2026-10-15``. A lone date is treated as the end of
    validity unless it is preceded by ``od``.
    """
    today = today or date.today()
    text = text or ""
    iso = [d for d in (_mk(*m) for m in _ISO_RE.findall(text)) if d]
    if len(iso) >= 2:
        return iso[0], iso[1]
    m = _SHARED_RANGE_RE.search(text)
    if m:
        d1, m1, d2, m2, y = m.groups()
        y2 = int(y) if y else today.year
        end = _mk(y2, m2, d2)
        start_month = int(m1) if m1 else int(m2)
        start = _mk(y2, start_month, d1)
        if start and end and start > end and not y:
            end = _mk(y2 + 1, m2, d2)
        if start and end:
            return start, end
    found = []
    for m in _DATE_RE.finditer(text):
        d, mo, y = m.groups()
        dt = _mk(int(y) if y else today.year, mo, d)
        if dt:
            found.append((dt, bool(y), _FROM_RE.search(text[: m.start()])))
    if not found and iso:
        return None, iso[0]
    if not found:
        return None, None
    if len(found) == 1:
        dt, _, is_from = found[0]
        return (dt, None) if is_from else (None, dt)
    start, end = found[0][0], found[1][0]
    if start > end and not found[0][1]:
        end = _mk(end.year + 1, end.month, end.day) or end
    return start, end


def _text(el) -> str:
    return el.get_text(" ", strip=True) if el else ""


def _iso_date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _pick(d: dict, *keys):
    for k in keys:
        if d.get(k):
            return d[k]
    return None


def _num(value) -> float | None:
    try:
        return float(str(value).replace(" ", "").replace("\u00a0", "").replace(",", "."))
    except ValueError:
        return None


def _walk_jsonld(node):
    if isinstance(node, list):
        for n in node:
            yield from _walk_jsonld(n)
    elif isinstance(node, dict):
        yield node
        for v in node.values():
            if isinstance(v, (dict, list)):
                yield from _walk_jsonld(v)


def _parse_jsonld(source: str, base: str, soup: BeautifulSoup) -> list[Offer]:
    """schema.org Product/Offer data embedded as JSON-LD (most reliable)."""
    offers: list[Offer] = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except ValueError:
            log.debug("%s: invalid JSON-LD block skipped", source)
            continue
        for node in _walk_jsonld(data):
            types = node.get("@type")
            types = types if isinstance(types, list) else [types]
            if "Product" not in types or not node.get("name"):
                continue
            raw = node.get("offers")
            for off in raw if isinstance(raw, list) else [raw or {}]:
                if not isinstance(off, dict):
                    continue
                seller = off.get("seller")
                store = seller.get("name") if isinstance(seller, dict) else seller
                price = _num(off.get("price", off.get("lowPrice")))
                if price is None:
                    continue
                url = off.get("url") or node.get("url")
                offers.append(Offer(
                    source, str(node["name"])[:300], str(store)[:100] if store else None, price,
                    _iso_date(_pick(off, "validFrom", "availabilityStarts", "validFromDate"))
                    or _iso_date(_pick(node, "validFrom", "availabilityStarts")),
                    _iso_date(_pick(off, "priceValidUntil", "validThrough", "validTo", "availabilityEnds"))
                    or _iso_date(_pick(node, "validThrough", "priceValidUntil", "validTo")),
                    urljoin(base, url) if url else None,
                ))
    return offers


def _prop(card, name: str):
    el = card.find(attrs={"itemprop": name})
    if not el:
        return None
    return el.get("content") or el.get("href") or _text(el)


def _parse_microdata(source: str, base: str, soup: BeautifulSoup) -> list[Offer]:
    offers: list[Offer] = []
    for card in soup.find_all(attrs={"itemtype": re.compile(r"schema\.org/Product$")}):
        name = _prop(card, "name")
        price = _num(_prop(card, "price"))
        if not name or price is None:
            continue
        url = _prop(card, "url")
        seller = card.find(attrs={"itemprop": "seller"})
        store = (_prop(seller, "name") if seller else None) or None
        offers.append(Offer(
            source, name[:300], store[:100] if store else None, price,
            *_card_dates(card, None),
            urljoin(base, url) if url else None,
        ))
    return offers


_VALID_ATTRS_FROM = ("data-valid-from", "data-validfrom", "data-from", "data-date-from")
_VALID_ATTRS_TO = ("data-valid-to", "data-validto", "data-valid-until", "data-to", "data-date-to")


def _card_dates(card, validity_el, today: date | None = None) -> tuple[date | None, date | None]:
    """Best-effort validity from microdata, attributes, <time>, validity element, then card text."""
    vf = _iso_date(_prop(card, "validFrom") or _prop(card, "availabilityStarts"))
    vt = _iso_date(_prop(card, "priceValidUntil") or _prop(card, "validThrough") or _prop(card, "availabilityEnds"))
    for el in [card, *card.find_all(True)]:
        vf = vf or next((_iso_date(el.get(a)) for a in _VALID_ATTRS_FROM if el.get(a)), None)
        vt = vt or next((_iso_date(el.get(a)) for a in _VALID_ATTRS_TO if el.get(a)), None)
    if vf and vt:
        return vf, vt
    times = [d for d in (_iso_date(t.get("datetime")) for t in card.find_all("time")) if d]
    if not (vf or vt) and times:
        return (times[0], times[1]) if len(times) > 1 else (None, times[0])
    if validity_el is not None:
        tf, tt = parse_dates(_text(validity_el), today)
    else:
        tf, tt = parse_dates(_text(card), today)
    return vf or tf, vt or tt


def _first(card, selectors: str):
    for sel in selectors.split("|"):
        el = card.select_one(sel.strip())
        if el:
            return el
    return None


def _parse_cards(source: str, base: str, soup: BeautifulSoup, spec: dict, today: date | None) -> list[Offer]:
    """Server-rendered markup parsed with explicit, site-specific selectors."""
    offers: list[Offer] = []
    for card in soup.select(spec["card"]):
        name_el = _first(card, spec["name"])
        price_el = _first(card, spec["price"])
        name = _text(name_el)
        price = parse_price(_text(price_el)) if price_el else None
        if not name or price is None:
            continue
        link = name_el if name_el is not None and name_el.name == "a" and name_el.get("href") else None
        link = link or (name_el.find("a", href=True) if name_el is not None else None) or card.find("a", href=True)
        store_el = _first(card, spec["store"])
        store = (store_el.get("alt") or store_el.get("title") or _text(store_el)) if store_el else None
        validity_el = _first(card, spec["validity"])
        vf, vt = _card_dates(card, validity_el, today)
        offers.append(Offer(
            source, name[:300], store[:100] if store else None, price, vf, vt,
            urljoin(base, link["href"]) if link else None,
        ))
    return offers


_KUPI = {
    "card": "div.discount_row, div.product_container div.discount, div.group_discounts > div[class*=discount]",
    "name": "a.product_link_history | .discount_product_name | .product_name | h2 a | h3 a",
    "price": ".discount_price_value | .discount_price | .price_value | .price",
    "store": ".discount_shop img | .shop_logo img | img[alt] | .discount_shop | .shop_name",
    "validity": ".discount_validity | .validity | .discount_date | .date",
}
_AKCNICENY = {
    "card": "div.product-box, div.product-item, div.offer-item, li.product-item, div.leaflet-product",
    "name": ".product-name a | .product-title a | h3 a | h2 a | .product-name | .product-title",
    "price": ".product-price | .price-current | .price | .cena",
    "store": ".shop-name | .store-name | .shop img | img.shop-logo | .shop",
    "validity": ".validity | .product-validity | .date | .platnost",
}


def _dedup(offers: list[Offer]) -> list[Offer]:
    seen, out = set(), []
    for o in offers:
        key = (o.product_name, o.store, o.price, o.url)
        if key not in seen:
            seen.add(key)
            out.append(o)
    return out


def _fill_page_dates(offers: list[Offer], soup: BeautifulSoup, today: date | None) -> None:
    """If offers lack any dates, use a full interval found in page headings."""
    if any(o.valid_from or o.valid_to for o in offers):
        return
    for h in soup.select("h1, h2"):
        vf, vt = parse_dates(_text(h), today)
        if vf and vt:
            for o in offers:
                o.valid_from, o.valid_to = vf, vt
            return


def _parse_site(source: str, base: str, spec: dict, html: str, today: date | None) -> list[Offer]:
    try:
        soup = BeautifulSoup(html, "html.parser")
        for strategy in (
            lambda: _parse_jsonld(source, base, soup),
            lambda: _parse_microdata(source, base, soup),
            lambda: _parse_cards(source, base, soup, spec, today),
        ):
            offers = _dedup(strategy())
            if offers:
                _fill_page_dates(offers, soup, today)
                return offers
        log.warning(
            "%s: no offers parsed (html length=%d, title=%r, start=%r)",
            source, len(html), _text(soup.title), html[:300],
        )
    except Exception:
        log.exception("%s: parser crashed (html length=%d)", source, len(html))
    return []


def parse_kupi(html: str, today: date | None = None) -> list[Offer]:
    return _parse_site("kupi.cz", "https://www.kupi.cz/", _KUPI, html, today)


def parse_akcniceny(html: str, today: date | None = None) -> list[Offer]:
    return _parse_site("akcniceny.cz", "https://www.akcniceny.cz/", _AKCNICENY, html, today)


PARSERS = {"kupi.cz": parse_kupi, "akcniceny.cz": parse_akcniceny}


def search(client: httpx.Client, source: str, query: str) -> list[Offer]:
    url = SOURCES[source].format(q=quote_plus(query))
    try:
        resp = client.get(url)
        resp.raise_for_status()
        return PARSERS[source](resp.text)
    except Exception:
        log.exception("Search failed: %s %r", source, query)
        return []


def make_client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": config.USER_AGENT, "Accept-Language": "cs"},
        timeout=20,
        follow_redirects=True,
    )
