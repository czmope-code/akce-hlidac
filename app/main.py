import hmac
import logging
import secrets
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import config, jobs
from .db import Base, engine, get_db
from .models import FoundOffer, TrackedItem

logging.basicConfig(level=logging.INFO)
BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))

MAX_ATTEMPTS, WINDOW = 5, 300
_attempts: dict[str, list[float]] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    sched = jobs.start_scheduler() if config.SCRAPE_ENABLED else None
    yield
    if sched:
        sched.shutdown(wait=False)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SECRET_KEY,
    https_only=config.COOKIE_SECURE,
    same_site="lax",
    max_age=7 * 24 * 3600,
)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _rate_limited(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _attempts.get(ip, []) if now - t < WINDOW]
    _attempts[ip] = recent
    return len(recent) >= MAX_ATTEMPTS


def _csrf(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = request.session["csrf"] = secrets.token_urlsafe(24)
    return token


def render(request: Request, name: str, **ctx):
    ctx["csrf"] = _csrf(request)
    ctx["user"] = request.session.get("user")
    return templates.TemplateResponse(request, name, ctx)


def require_user(request: Request, csrf: str | None = Form(None)):
    if not request.session.get("user"):
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    if request.method == "POST" and not hmac.compare_digest(
        csrf or "", request.session.get("csrf", "")
    ):
        raise HTTPException(status_code=403, detail="Bad CSRF token")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


class OfferOut(BaseModel):
    item: str
    description: str
    store: str | None = None
    price: float | None = None
    valid_from: date | None = None
    valid_to: date | None = None


class ItemOut(BaseModel):
    name: str
    offers: list[OfferOut]


class ItemsSummary(BaseModel):
    updated_at: datetime
    items: list[ItemOut]


def _is_active(offer: FoundOffer, today: date) -> bool:
    return offer.valid_to is None or offer.valid_to >= today


def _offer_sort_key(offer: FoundOffer):
    # lowest price first, unpriced last
    return (offer.price is None, float(offer.price or 0))


@app.get("/api/items/summary", response_model=ItemsSummary)
def items_summary(db: Session = Depends(get_db)):
    today = date.today()
    items = db.scalars(select(TrackedItem).order_by(TrackedItem.name)).all()
    grouped: dict[int, list[FoundOffer]] = {}
    latest: datetime | None = None
    for o in db.scalars(select(FoundOffer)).all():
        if o.discovered_at and (latest is None or o.discovered_at > latest):
            latest = o.discovered_at
        if _is_active(o, today):
            grouped.setdefault(o.item_id, []).append(o)
    out = []
    for item in items:
        offers = sorted(grouped.get(item.id, []), key=_offer_sort_key)
        if not offers:
            continue
        out.append(ItemOut(name=item.name, offers=[
            OfferOut(item=item.name, description=o.product_name, store=o.store,
                     price=float(o.price) if o.price is not None else None,
                     valid_from=o.valid_from, valid_to=o.valid_to)
            for o in offers
        ]))
    return ItemsSummary(updated_at=latest or datetime.now(timezone.utc), items=out)


@app.get("/login")
def login_form(request: Request):
    return render(request, "login.html", error=None)


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    ip = _client_ip(request)
    if _rate_limited(ip):
        return render(request, "login.html", error="Příliš mnoho pokusů, zkuste později.")
    ok = bool(config.ADMIN_PASSWORD) and (
        hmac.compare_digest(username.encode(), config.ADMIN_USER.encode())
        & hmac.compare_digest(password.encode(), config.ADMIN_PASSWORD.encode())
    )
    if not ok:
        _attempts.setdefault(ip, []).append(time.time())
        return render(request, "login.html", error="Neplatné přihlášení.")
    request.session.clear()
    request.session["user"] = config.ADMIN_USER
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request, _=Depends(require_user)):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/")
def dashboard(
    request: Request,
    item_id: int | None = None,
    source: str | None = None,
    store: str | None = None,
    db: Session = Depends(get_db),
    _=Depends(require_user),
):
    items = db.scalars(select(TrackedItem).order_by(TrackedItem.name)).all()
    q = select(FoundOffer).order_by(FoundOffer.discovered_at.desc()).limit(300)
    if item_id:
        q = q.where(FoundOffer.item_id == item_id)
    if source:
        q = q.where(FoundOffer.source == source)
    if store:
        q = q.where(FoundOffer.store == store)
    offers = db.scalars(q).all()
    names = {i.id: i.name for i in items}
    return render(request, "dashboard.html", items=items, offers=offers, names=names,
                  item_id=item_id, source=source, store=store or "")


@app.get("/items")
def items_page(request: Request, db: Session = Depends(get_db), _=Depends(require_user)):
    items = db.scalars(select(TrackedItem).order_by(TrackedItem.name)).all()
    return render(request, "items.html", items=items)


@app.post("/items")
def add_item(name: str = Form(...), db: Session = Depends(get_db), _=Depends(require_user)):
    name = name.strip()[:200]
    if name:
        db.add(TrackedItem(name=name))
        db.commit()
    return RedirectResponse("/items", status_code=303)


def _get_item(db: Session, item_id: int) -> TrackedItem:
    item = db.get(TrackedItem, item_id)
    if not item:
        raise HTTPException(404)
    return item


@app.post("/items/{item_id}/edit")
def edit_item(item_id: int, name: str = Form(...), db: Session = Depends(get_db),
              _=Depends(require_user)):
    item = _get_item(db, item_id)
    if name.strip():
        item.name = name.strip()[:200]
        db.commit()
    return RedirectResponse("/items", status_code=303)


@app.post("/items/{item_id}/toggle")
def toggle_item(item_id: int, db: Session = Depends(get_db), _=Depends(require_user)):
    item = _get_item(db, item_id)
    item.enabled = not item.enabled
    db.commit()
    return RedirectResponse("/items", status_code=303)


@app.post("/items/{item_id}/delete")
def delete_item(item_id: int, db: Session = Depends(get_db), _=Depends(require_user)):
    db.delete(_get_item(db, item_id))
    db.commit()
    return RedirectResponse("/items", status_code=303)


@app.post("/search-now")
def search_now(_=Depends(require_user)):
    threading.Thread(target=jobs.run_searches, daemon=True).start()
    return RedirectResponse("/", status_code=303)
