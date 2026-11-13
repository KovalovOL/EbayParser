"""Shared eBay state is independent of process liveness and survives worker restarts."""
from contextlib import contextmanager

from sqlalchemy import or_, select

from app.db import session_scope, stamp
from app.messages import display_error
from app.models import MonitorState, Watch
from app.platforms import PLATFORM_NAMES


def align_access_retries(db, retry_at):
    # A shared access failure has one deadline. Keep healthy searches with a later
    # normal schedule, but remove the old per-search exponential access backoff.
    candidates = db.scalars(select(Watch).where(Watch.active.is_(True), Watch.platforms == ["ebay"], or_(
        Watch.next_check_at <= retry_at, Watch.error.is_not(None))))
    for watch in candidates:
        watch.next_check_at = retry_at
        if "ebay" in watch.platform_state:
            watch.platform_state = {"ebay": watch.platform_state["ebay"] | {"next_check_at": retry_at}}


def record_result(retry_at=0, error=None):
    with session_scope() as db:
        state = db.get(MonitorState, 1)
        if state is None:
            state = MonitorState(id=1)
            db.add(state)
        state.retry_at, state.error = retry_at, error
        if retry_at:
            align_access_retries(db, retry_at)


def record_browser(browser):
    with session_scope() as db:
        state = db.get(MonitorState, 1)
        if state is None:
            state = MonitorState(id=1)
            db.add(state)
        state.browser = browser


def restore_monitor(ebay):
    with session_scope() as db:
        state = db.get(MonitorState, 1)
        if state:
            ebay.blocked_until = state.retry_at
            state.checking_watch_id = None
            state.browser = state.platform = None
            if state.retry_at:
                align_access_retries(db, max(stamp(), state.retry_at))
        # An older version could clear the shared pause after another search
        # succeeded while leaving a failed search's 40-60 minute backoff behind.
        deadline = max(stamp(), state.retry_at if state else 0)
        for watch in db.scalars(select(Watch).where(Watch.active.is_(True), Watch.platforms == ["ebay"], Watch.error.is_not(None),
                                                   Watch.next_check_at > deadline)):
            error = (display_error(watch.error) or "").casefold()
            if any(marker in error for marker in (
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
    only_ebay = bool(active) and all(watch.platforms == ["ebay"] for watch in active)
    ebay_retry_at = state.retry_at if ebay_active and state and state.retry_at > now else None
    retry_at = ebay_retry_at if only_ebay else None
    own_check = bool(state and state.checking_watch_id in {watch.id for watch in active})
    next_check = min((max(watch.next_check_at, ebay_retry_at or 0) if watch.platforms == ["ebay"]
                      else watch.next_check_at for watch in active), default=None)
    result = {"retry_at": retry_at, "next_check_at": max(next_check, retry_at or 0) if next_check else None,
              "active_searches": len(active), "failed_searches": sum(bool(w.error) for w in active),
              "error": None, "browser": state.browser if state else None,
              "ebay_retry_at": ebay_retry_at,
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
        result["error"] = state.error
    elif own_check:
        code = "checking"
    elif all(watch.error for watch in active):
        code = "retrying"
    else:
        code = "monitoring"
    return result | {"state": code}
