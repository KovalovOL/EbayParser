import hmac
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db, init_db, stamp
from app.models import LoginChallenge, Notification, RefreshSession, ServiceState, User, Watch, WatchItem
from app.schemas import LoginStart, LoginVerify, WatchInput, WatchToggle
from app.security import current_user, digest, new_tokens, rotate_refresh
from app.telegram import TelegramClient, TelegramError

settings = get_settings()
ROOT = Path(__file__).parent
templates = Jinja2Templates(directory=ROOT / "templates")


@asynccontextmanager
async def lifespan(app):
    init_db()
    settings.signing_key()
    app.state.telegram = TelegramClient()
    app.state.bot_username = settings.telegram_bot_username.lstrip("@")
    yield
    await app.state.telegram.close()


app = FastAPI(title="eBay Signal", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.middleware("http")
async def browser_security(request: Request, call_next):
    if request.method in {"POST", "PATCH", "PUT", "DELETE"}:
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != settings.public_url.rstrip("/"):
            return JSONResponse({"detail": "Недозволене джерело запиту"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data: https://i.ebayimg.com https://*.ebayimg.com; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/api/health")
def health(db: Session = Depends(get_db)):
    db.execute(select(1))
    return {"status": "ok"}


@app.get("/api/config")
async def public_config(request: Request):
    error = None
    if not app.state.bot_username:
        try:
            me = await app.state.telegram.call("getMe")
            app.state.bot_username = me["username"]
        except TelegramError as exc:
            error = str(exc)
    return {"bot_username": app.state.bot_username, "telegram_ready": bool(settings.telegram_bot_token),
            "error": error}


def set_tokens(response: Response, tokens):
    response.set_cookie("refresh_token", tokens[1], httponly=True, secure=settings.cookie_secure,
                        samesite="strict", path="/api/auth", max_age=settings.refresh_days * 86400)
    response.headers["Cache-Control"] = "no-store"
    return {"access_token": tokens[0], "token_type": "bearer", "expires_in": settings.access_minutes * 60}


@app.post("/api/auth/start")
async def start_login(payload: LoginStart, request: Request, db: Session = Depends(get_db)):
    if not settings.telegram_bot_token:
        raise HTTPException(503, "Додайте TELEGRAM_BOT_TOKEN або tg у .env")
    if not app.state.bot_username:
        try:
            app.state.bot_username = (await app.state.telegram.call("getMe"))["username"]
        except TelegramError as exc:
            raise HTTPException(503, str(exc)) from None
    now = stamp()
    ip = request.client.host if request.client else "unknown"
    recent = db.scalar(select(func.count()).select_from(LoginChallenge).where(
        LoginChallenge.username == payload.username, LoginChallenge.created_at > now - 60))
    count = db.scalar(select(func.count()).select_from(LoginChallenge).where(
        LoginChallenge.ip == ip, LoginChallenge.created_at > now - 3600))
    if recent or count >= 20:
        raise HTTPException(429, "Забагато запитів. Зачекайте хвилину перед новим кодом.")
    challenge = LoginChallenge(id=secrets.token_urlsafe(24), username=payload.username, ip=ip,
                               expires_at=now + 600)
    db.add(challenge)
    db.commit()
    return {"challenge_id": challenge.id, "expires_in": 600,
            "bot_url": f"https://t.me/{app.state.bot_username}?start=login_{challenge.id}"}


@app.post("/api/auth/verify")
def verify_login(payload: LoginVerify, response: Response, db: Session = Depends(get_db)):
    challenge = db.get(LoginChallenge, payload.challenge_id)
    if not challenge or challenge.used or challenge.expires_at < stamp() or challenge.attempts >= 5:
        raise HTTPException(400, "Код недійсний або закінчився. Створіть новий запит.")
    if not challenge.code_hash:
        raise HTTPException(400, "Спочатку відкрийте бота за посиланням і натисніть Start")
    # Claim an attempt atomically before checking the OTP, including concurrent requests.
    claimed = db.execute(update(LoginChallenge).where(
        LoginChallenge.id == challenge.id, LoginChallenge.attempts < 5,
        LoginChallenge.used.is_(False)).values(attempts=LoginChallenge.attempts + 1))
    if claimed.rowcount != 1:
        db.rollback()
        raise HTTPException(400, "Запит уже використано")
    if not hmac.compare_digest(challenge.code_hash, digest(challenge.id + ":" + payload.code)):
        db.commit()
        raise HTTPException(400, "Невірний код підтвердження")
    challenge.used = True
    user = db.scalar(select(User).where(User.telegram_id == challenge.telegram_id))
    if user is None:
        user = User(telegram_id=challenge.telegram_id, chat_id=challenge.chat_id, username=challenge.username)
        db.add(user)
        db.flush()
    else:
        user.username, user.chat_id = challenge.username, challenge.chat_id
    tokens = new_tokens(db, user)
    db.commit()
    return set_tokens(response, tokens)


@app.post("/api/auth/refresh")
def refresh(request: Request, response: Response, db: Session = Depends(get_db)):
    token = request.cookies.get("refresh_token")
    if not token:
        raise HTTPException(401, "Немає активної сесії")
    return set_tokens(response, rotate_refresh(db, token))


@app.post("/api/auth/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    token = request.cookies.get("refresh_token")
    current = db.get(RefreshSession, digest(token)) if token else None
    if current:
        db.execute(update(RefreshSession).where(RefreshSession.family == current.family).values(revoked=True))
        db.commit()
    response.delete_cookie("refresh_token", path="/api/auth", secure=settings.cookie_secure, samesite="strict")
    return {"ok": True}


@app.get("/api/me")
def me(user: User = Depends(current_user)):
    return {"id": user.id, "username": user.username}


@app.get("/api/status")
def status(user: User = Depends(current_user), db: Session = Depends(get_db)):
    states = {s.name: {"online": stamp() - s.heartbeat_at < 120, "last_seen": s.heartbeat_at, "error": s.error}
              for s in db.scalars(select(ServiceState))}
    return {"services": states, "strict_newness": True}


def own_watch(db, user, watch_id):
    watch = db.scalar(select(Watch).where(Watch.id == watch_id, Watch.user_id == user.id))
    if not watch:
        raise HTTPException(404, "Пошук не знайдено")
    return watch


def watch_dict(watch, counts=None):
    return {k: getattr(watch, k) for k in (
        "id", "name", "query", "filters", "interval_seconds", "active", "initialized", "created_at",
        "tracking_since", "last_checked_at", "next_check_at", "error", "search_url", "truncated"
    )} | {"counts": counts or {}}


@app.get("/api/watches")
def list_watches(user: User = Depends(current_user), db: Session = Depends(get_db)):
    watches = db.scalars(select(Watch).where(Watch.user_id == user.id).order_by(Watch.created_at.desc())).all()
    results = []
    for watch in watches:
        total = db.scalar(select(func.count()).select_from(WatchItem).where(
            WatchItem.watch_id == watch.id, WatchItem.matches.is_(True)))
        fresh = db.scalar(select(func.count()).select_from(WatchItem).where(
            WatchItem.watch_id == watch.id, WatchItem.matches.is_(True), WatchItem.is_new.is_(True)))
        pending = db.scalar(select(func.count()).select_from(WatchItem).where(
            WatchItem.watch_id == watch.id, WatchItem.matches.is_(None)))
        results.append(watch_dict(watch, {"total": total, "new": fresh, "pending": pending}))
    return results


@app.post("/api/watches", status_code=201)
def create_watch(payload: WatchInput, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if db.scalar(select(func.count()).select_from(Watch).where(Watch.user_id == user.id)) >= 30:
        raise HTTPException(400, "Максимум 30 пошуків на користувача")
    watch = Watch(user_id=user.id, name=payload.name, query=payload.query,
                  filters=payload.filters.model_dump(), interval_seconds=payload.interval_seconds)
    db.add(watch)
    db.commit()
    return watch_dict(watch)


@app.put("/api/watches/{watch_id}")
def edit_watch(watch_id: int, payload: WatchInput, user: User = Depends(current_user), db: Session = Depends(get_db)):
    watch = own_watch(db, user, watch_id)
    changed = watch.query != payload.query or watch.filters != payload.filters.model_dump()
    watch.name, watch.query = payload.name, payload.query
    watch.filters, watch.interval_seconds = payload.filters.model_dump(), payload.interval_seconds
    if changed:
        # Changing criteria creates a new baseline, cancelling outstanding old notifications via FK cascade.
        db.execute(delete(WatchItem).where(WatchItem.watch_id == watch.id))
        watch.initialized, watch.tracking_since = False, stamp()
        watch.revision += 1
        watch.error, watch.failures = None, 0
    watch.next_check_at = stamp()
    db.commit()
    return watch_dict(watch)


@app.patch("/api/watches/{watch_id}")
def toggle_watch(watch_id: int, payload: WatchToggle, user: User = Depends(current_user), db: Session = Depends(get_db)):
    watch = own_watch(db, user, watch_id)
    if watch.active != payload.active:
        watch.active = payload.active
        watch.revision += 1
        if payload.active:
            # Listings appearing while paused belong to the new baseline.
            watch.tracking_since, watch.initialized, watch.next_check_at = stamp(), False, stamp()
        else:
            db.execute(delete(Notification).where(Notification.item_id.in_(
                select(WatchItem.id).where(WatchItem.watch_id == watch.id)), Notification.sent_at.is_(None)))
    db.commit()
    return watch_dict(watch)


@app.post("/api/watches/{watch_id}/check")
def check_watch(watch_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    watch = own_watch(db, user, watch_id)
    if not watch.active:
        raise HTTPException(400, "Спочатку відновіть пошук")
    if watch.last_checked_at and stamp() - watch.last_checked_at < 60:
        raise HTTPException(429, "Ручна перевірка доступна раз на хвилину")
    watch.next_check_at = stamp()
    db.commit()
    return {"ok": True}


@app.delete("/api/watches/{watch_id}", status_code=204)
def delete_watch(watch_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.delete(own_watch(db, user, watch_id))
    db.commit()
    return Response(status_code=204)


@app.get("/api/items")
def list_items(watch_id: int | None = None, only_new: bool = False, offset: int = 0, limit: int = 30,
               user: User = Depends(current_user), db: Session = Depends(get_db)):
    if offset < 0 or not 1 <= limit <= 100:
        raise HTTPException(422, "Некоректна пагінація")
    query = select(WatchItem, Watch.name).join(Watch).where(Watch.user_id == user.id, WatchItem.matches.is_(True))
    if watch_id is not None:
        own_watch(db, user, watch_id)
        query = query.where(Watch.id == watch_id)
    if only_new:
        query = query.where(WatchItem.is_new.is_(True))
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.execute(query.order_by(WatchItem.first_seen_at.desc(), WatchItem.id.desc()).offset(offset).limit(limit))
    items = []
    for item, name in rows:
        notification = db.scalar(select(Notification).where(Notification.item_id == item.id))
        items.append(item.data | {"id": item.id, "watch_id": item.watch_id, "watch_name": name,
                                  "baseline": item.baseline, "is_new": item.is_new,
                                  "first_seen_at": item.first_seen_at, "last_seen_at": item.last_seen_at,
                                  "detailed_at": item.detailed_at, "detail_error": item.detail_error,
                                  "notification_sent": bool(notification and notification.sent_at),
                                  "notification_error": notification.error if notification else None})
    return {"items": items, "total": total, "offset": offset, "limit": limit}


@app.post("/api/items/{item_id}/details")
def request_details(item_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    item = db.scalar(select(WatchItem).join(Watch).where(WatchItem.id == item_id, Watch.user_id == user.id))
    if item is None:
        raise HTTPException(404, "Товар не знайдено")
    watch = db.get(Watch, item.watch_id)
    if not watch.active:
        raise HTTPException(400, "Відновіть пошук для завантаження характеристик")
    if item.detailed_at and stamp() - item.detailed_at < 300:
        return {"ok": True, "message": "Характеристики вже оновлено"}
    # Prioritize this item without fetching arbitrary URLs from the API process.
    item.detailed_at = None
    item.detail_error = None
    watch.next_check_at = stamp()
    db.commit()
    return {"ok": True, "message": "Характеристики додано в чергу; зачекайте наступної перевірки"}
