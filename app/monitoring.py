"""Shared source recovery state survives worker restarts independently of liveness."""
from contextlib import contextmanager
from math import ceil

from sqlalchemy import select

from app.db import session_scope, stamp
from app.messages import display_error
from app.models import MonitorState, ServiceState, Watch
from app.platforms import PLATFORM_NAMES


def platform_next_check(watch, platform):
    return watch.platform_state.get(platform, {}).get(
        "next_check_at", watch.next_check_at if len(watch.platforms) == 1 else 0)


def save_access_state(db, platform, client):
    if client is None or not hasattr(client, "access_failures"):
        return
    key = f"access_failures:{platform}"
    state = db.get(ServiceState, key)
    if state is None:
        state = ServiceState(name=key)
        db.add(state)
    state.value = client.access_failures
    if platform == "ebay":
        browser = db.get(ServiceState, "preferred_browser:ebay")
        if browser is None:
            browser = ServiceState(name="preferred_browser:ebay")
            db.add(browser)
        browser.value = int(client.preferred_browser == "firefox")


def restore_access_state(db, platform, client):
    if state := db.get(ServiceState, f"access_failures:{platform}"):
        client.access_failures = max(0, state.value)
    if platform == "ebay":
        if browser := db.get(ServiceState, "preferred_browser:ebay"):
            client.preferred_browser = "firefox" if browser.value == 1 else "chromium"
    elif state := db.get(ServiceState, f"access:{platform}"):
        client.blocked_until = state.value


def record_market_result(platform, client, retry_at=0):
    with session_scope() as db:
        key = f"access:{platform}"
        state = db.get(ServiceState, key)
        if state is None:
            state = ServiceState(name=key)
            db.add(state)
        # Round up so restarting cannot shorten the agreed quiet window.
        state.value = ceil(retry_at)
        save_access_state(db, platform, client)


def record_result(retry_at=0, error=None, *, client=None):
    with session_scope() as db:
        state = db.get(MonitorState, 1)
        if state is None:
            state = MonitorState(id=1)
            db.add(state)
        state.retry_at, state.error = retry_at, error
        save_access_state(db, "ebay", client)
        # A service pause gates requests, but must not rewrite other searches'
        # schedules: doing so lets one failing search starve all the others.


def record_browser(browser):
    with session_scope() as db:
        state = db.get(MonitorState, 1)
        if state is None:
            state = MonitorState(id=1)
            db.add(state)
        state.browser = browser


def restore_monitor(ebay):
    with session_scope() as db:
        restore_access_state(db, "ebay", ebay)
        state = db.get(MonitorState, 1)
        if state:
            ebay.blocked_until = state.retry_at
            state.checking_watch_id = None
            state.browser = state.platform = None
        # An older version could clear the shared pause after another search
        # succeeded while leaving a failed search's 40-60 minute backoff behind.
        deadline = max(stamp(), state.retry_at if state else 0)
        for watch in db.scalars(select(Watch).where(Watch.active.is_(True), Watch.platforms == ["ebay"])):
            # Older shared pauses moved untouched legacy searches forever while
            # leaving their last_checked_at and error unchanged.
            if not watch.platform_state and watch.last_checked_at:
                watch.next_check_at = min(watch.next_check_at, watch.last_checked_at + watch.interval_seconds)
            error = (display_error(watch.error) or "").casefold()
            if watch.next_check_at > deadline and any(marker in error for marker in (
                "http 403", "http 429", "http 503", "human verification", "browser verification",
                "verification page", "access is restricted", "all ebay requests are paused",
            )):
                watch.next_check_at = deadline
                if "ebay" in watch.platform_state:
                    watch.platform_state = {"ebay": watch.platform_state["ebay"] | {"next_check_at": deadline}}


@contextmanager
def checking(watch_id, platform="ebay"):
    with session_scope() as db:
        state = db.get(MonitorState, 1)
        if state is None:
            state = MonitorState(id=1)
            db.add(state)
        state.checking_watch_id, state.platform = watch_id, platform
    try:
        yield
    finally:
        with session_scope() as db:
            state = db.get(MonitorState, 1)
            if state and state.checking_watch_id == watch_id:
                state.checking_watch_id = state.platform = None


def monitoring_status(db, user_id, services):
    now = stamp()
    watches = list(db.scalars(select(Watch).where(Watch.user_id == user_id)))
    active = [watch for watch in watches if watch.active]
    state = db.get(MonitorState, 1)
    ebay_active = [watch for watch in active if "ebay" in watch.platforms]
    platforms = {platform for watch in active for platform in watch.platforms}
    ebay_retry_at = state.retry_at if ebay_active and state and state.retry_at > now else None
    platform_retries = {"ebay": ebay_retry_at} if ebay_retry_at else {}
    for platform in platforms - {"ebay"}:
        access = db.get(ServiceState, f"access:{platform}")
        if access and access.value > now:
            platform_retries[platform] = access.value
    retry_at = min(platform_retries.values()) if platforms and platforms <= platform_retries.keys() else None
    own_check = bool(state and state.checking_watch_id in {watch.id for watch in active})
    next_check = min((max(platform_next_check(watch, platform), platform_retries.get(platform, 0))
                      for watch in active for platform in watch.platforms), default=None)
    result = {"retry_at": retry_at, "next_check_at": max(now, next_check, retry_at or 0) if next_check is not None else None,
              "active_searches": len(active), "failed_searches": sum(bool(w.error) for w in active),
              "error": None, "browser": state.browser if state else None,
              "ebay_retry_at": ebay_retry_at,
              "platform_retries": platform_retries,
              "checking_platform": PLATFORM_NAMES.get(state.platform) if own_check else None}
    worker = services.get("worker", {})
    if not worker.get("online"):
        code = "offline"
    elif worker.get("error"):
        code = "error"
        result["error"] = worker["error"]
    elif not watches:
        code = "no_searches"
    elif not active:
        code = "paused"
    elif retry_at:
        code = "restricted"
        result["error"] = state.error if ebay_retry_at and state else "Source access is restricted. Retrying automatically."
    elif own_check:
        code = "checking"
    elif all(watch.error for watch in active):
        code = "retrying"
    elif platform_retries or any(watch.error for watch in active):
        code = "degraded"
    else:
        code = "monitoring"
    return result | {"state": code}
