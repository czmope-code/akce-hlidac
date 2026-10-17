"""Scrapers for kupi.cz and akcniceny.cz.

Parsing is best-effort and deliberately defensive: the HTML structure of the
sites may change, so each parser returns an empty list on failure. Extend
``parse_*`` functions to improve accuracy.
"""
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


def parse_dates(text: str, today: date | None = None) -> tuple[date | None, date | None]:
    today = today or date.today()
    found = []
    for d, mo, y in _DATE_RE.findall(text):
        try:
            found.append(date(int(y) if y else today.year, int(mo), int(d)))
        except ValueError:
            continue
    if not found:
        return None, None
    if len(found) == 1:
        return None, found[0]
    return found[0], found[1]


def _parse_generic(source: str, base: str, html: str, today: date | None = None) -> list[Offer]:
    soup = BeautifulSoup(html, "html.parser")
    offers: list[Offer] = []
    cards = soup.select("[class*=product], [class*=offer], [class*=discount], article")
    seen = set()
    for card in cards:
        if card.find(attrs={"class": re.compile("product|offer|discount")}) and card.name != "article":
            continue  # only innermost cards
        title_el = card.find(["h2", "h3", "h4"]) or card.find("a")
        if not title_el:
            continue
        name = title_el.get_text(" ", strip=True)
        text = card.get_text(" ", strip=True)
        price = parse_price(text)
        if not name or price is None:
            continue
        link = card.find("a", href=True)
        url = urljoin(base, link["href"]) if link else None
        store_el = card.select_one("[class*=shop], [class*=store], [class*=obchod]")
        store = store_el.get_text(" ", strip=True)[:100] if store_el else None
        vf, vt = parse_dates(text, today)
        key = (name, store, price, url)
        if key in seen:
            continue
        seen.add(key)
        offers.append(Offer(source, name[:300], store, price, vf, vt, url))
    return offers


def parse_kupi(html: str, today: date | None = None) -> list[Offer]:
    return _parse_generic("kupi.cz", "https://www.kupi.cz/", html, today)


def parse_akcniceny(html: str, today: date | None = None) -> list[Offer]:
    return _parse_generic("akcniceny.cz", "https://www.akcniceny.cz/", html, today)


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
