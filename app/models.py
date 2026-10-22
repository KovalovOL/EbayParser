from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, stamp


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(unique=True)
    chat_id: Mapped[int]
    username: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[float] = mapped_column(default=stamp)


class LoginChallenge(Base):
    __tablename__ = "login_challenges"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), index=True)
    ip: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[float] = mapped_column(default=stamp)
    expires_at: Mapped[float]
    code_hash: Mapped[str | None] = mapped_column(String(64))
    telegram_id: Mapped[int | None]
    chat_id: Mapped[int | None]
    attempts: Mapped[int] = mapped_column(default=0)
    used: Mapped[bool] = mapped_column(default=False)


class RefreshSession(Base):
    __tablename__ = "refresh_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    family: Mapped[str] = mapped_column(String(64), index=True)
    expires_at: Mapped[float]
    used: Mapped[bool] = mapped_column(default=False)
    revoked: Mapped[bool] = mapped_column(default=False)


class Watch(Base):
    __tablename__ = "watches"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    query: Mapped[str] = mapped_column(String(200))
    filters: Mapped[dict] = mapped_column(JSON, default=dict)
    interval_seconds: Mapped[int] = mapped_column(default=300)
    active: Mapped[bool] = mapped_column(default=True)
    initialized: Mapped[bool] = mapped_column(default=False)
    revision: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[float] = mapped_column(default=stamp)
    tracking_since: Mapped[float] = mapped_column(default=stamp)
    last_checked_at: Mapped[float | None]
    next_check_at: Mapped[float] = mapped_column(default=stamp, index=True)
    error: Mapped[str | None] = mapped_column(Text)
    failures: Mapped[int] = mapped_column(default=0)
    search_url: Mapped[str | None] = mapped_column(Text)
    truncated: Mapped[bool] = mapped_column(default=False)


class WatchItem(Base):
    __tablename__ = "watch_items"
    __table_args__ = (UniqueConstraint("watch_id", "ebay_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    watch_id: Mapped[int] = mapped_column(ForeignKey("watches.id", ondelete="CASCADE"), index=True)
    ebay_id: Mapped[str] = mapped_column(String(32))
    data: Mapped[dict] = mapped_column(JSON)
    baseline: Mapped[bool] = mapped_column(Boolean)
    matches: Mapped[bool | None] = mapped_column(Boolean)
    is_new: Mapped[bool] = mapped_column(default=False)
    first_seen_at: Mapped[float] = mapped_column(default=stamp)
    last_seen_at: Mapped[float] = mapped_column(default=stamp)
    detailed_at: Mapped[float | None]
    detail_error: Mapped[str | None] = mapped_column(Text)


class SeenListing(Base):
    __tablename__ = "seen_listings"
    __table_args__ = (UniqueConstraint("user_id", "fingerprint"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(Text)
    price: Mapped[str] = mapped_column(String(128))
    currency: Mapped[str] = mapped_column(String(8))
    location: Mapped[str] = mapped_column(Text)
    first_seen_at: Mapped[float] = mapped_column(default=stamp)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("watch_items.id", ondelete="CASCADE"), index=True)
    seen_listing_id: Mapped[int | None] = mapped_column(
        ForeignKey("seen_listings.id", ondelete="CASCADE"), unique=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[float] = mapped_column(default=stamp, index=True)
    sent_at: Mapped[float | None]
    error: Mapped[str | None] = mapped_column(Text)


class ServiceState(Base):
    __tablename__ = "service_state"
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    heartbeat_at: Mapped[float] = mapped_column(Float, default=stamp)
    value: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)


class MonitorState(Base):
    __tablename__ = "monitor_state"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    retry_at: Mapped[float] = mapped_column(default=0)
    checking_watch_id: Mapped[int | None] = mapped_column(ForeignKey("watches.id", ondelete="SET NULL"))
    error: Mapped[str | None] = mapped_column(Text)
    browser: Mapped[str | None] = mapped_column(String(16))

