import hashlib
import hmac
import secrets
import time
from datetime import timedelta

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db, utcnow
from app.models import RefreshSession, User

bearer = HTTPBearer(auto_error=False)


def digest(value: str) -> str:
    return hmac.new(get_settings().signing_key().encode(), value.encode(), hashlib.sha256).hexdigest()


def new_tokens(db: Session, user: User, family: str | None = None) -> tuple[str, str]:
    settings = get_settings()
    now = utcnow()
    access = jwt.encode({"sub": str(user.id), "iat": now, "exp": now + timedelta(minutes=settings.access_minutes),
                         "type": "access", "iss": "ebay-signal", "aud": "ebay-signal-web"},
                        settings.signing_key(), algorithm="HS256")
    refresh = secrets.token_urlsafe(48)
    db.add(RefreshSession(token_hash=digest(refresh), user_id=user.id, family=family or secrets.token_urlsafe(24),
                          expires_at=time.time() + settings.refresh_days * 86400))
    return access, refresh


def rotate_refresh(db: Session, token: str) -> tuple[str, str]:
    current = db.get(RefreshSession, digest(token))
    if not current or current.revoked or current.expires_at < time.time():
        raise HTTPException(401, "Your session has expired. Sign in with Telegram.")
    if current.used:
        db.execute(update(RefreshSession).where(RefreshSession.family == current.family).values(revoked=True))
        db.commit()
        raise HTTPException(401, "This session is no longer valid. Sign in again.")
    # Conditional claim also prevents two concurrent refreshes from both rotating the same token.
    claimed = db.execute(update(RefreshSession).where(
        RefreshSession.token_hash == current.token_hash, RefreshSession.used.is_(False),
        RefreshSession.revoked.is_(False)).values(used=True))
    if claimed.rowcount != 1:
        db.rollback()
        raise HTTPException(401, "This session has already been refreshed. Sign in again.")
    user = db.get(User, current.user_id)
    if not user:
        raise HTTPException(401, "User not found")
    tokens = new_tokens(db, user, current.family)
    db.commit()
    return tokens


def current_user(auth: HTTPAuthorizationCredentials | None = Depends(bearer), db: Session = Depends(get_db)) -> User:
    if not auth:
        raise HTTPException(401, "Sign in with Telegram")
    try:
        claims = jwt.decode(auth.credentials, get_settings().signing_key(), algorithms=["HS256"],
                            issuer="ebay-signal", audience="ebay-signal-web",
                            options={"require": ["exp", "sub", "iat", "type"]})
        if claims["type"] != "access":
            raise ValueError()
        user_id = int(claims["sub"])
    except (jwt.PyJWTError, ValueError, KeyError):
        raise HTTPException(401, "The access token has expired or is invalid.") from None
    user = db.scalar(select(User).where(User.id == user_id))
    if not user:
        raise HTTPException(401, "User not found")
    return user

