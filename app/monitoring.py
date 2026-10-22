"""Shared eBay state is independent of process liveness and survives worker restarts."""
from contextlib import contextmanager

from sqlalchemy import or_, select, update

from app.db import session_scope, stamp
from app.messages import display_error
from app.models import MonitorState, Watch


def align_access_retries(db, retry_at):
    # A shared access failure has one deadline. Keep healthy searches with a later
    # normal schedule, but remove the old per-search exponential access backoff.
    db.execute(update(Watch).where(Watch.active.is_(True), or_(
        Watch.next_check_at <= retry_at, Watch.error.is_not(None)
    )).values(next_check_at=retry_at))


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
            state.browser = None
            if state.retry_at:
                align_access_retries(db, max(stamp(), state.retry_at))
        # An older version could clear the shared pause after another search
        # succeeded while leaving a failed search's 40-60 minute backoff behind.
        deadline = max(stamp(), state.retry_at if state else 0)
        for watch in db.scalars(select(Watch).where(Watch.active.is_(True), Watch.error.is_not(None),
                                                   Watch.next_check_at > deadline)):
            error = (display_error(watch.error) or "").casefold()
            if any(marker in error for marker in (
                "http 403", "http 429", "http 503", "human verification", "browser verification",
                "verification page", "access is restricted", "all ebay requests are paused",
            )):
                watch.next_check_at = deadline


@contextmanager
def checking(watch_id):
    with session_scope() as db:
        state = db.get(MonitorState, 1)
        if state is None:
            state = MonitorState(id=1)
            db.add(state)
        state.checking_watch_id = watch_id
    try:
        yield
    finally:
        with session_scope() as db:
            state = db.get(MonitorState, 1)
            if state and state.checking_watch_id == watch_id:
                state.checking_watch_id = None


def monitoring_status(db, user_id, services):
    now = stamp()
    watches = list(db.scalars(select(Watch).where(Watch.user_id == user_id)))
    active = [watch for watch in watches if watch.active]
    state = db.get(MonitorState, 1)
    retry_at = state.retry_at if state and state.retry_at > now else None
    next_check = min((watch.next_check_at for watch in active), default=None)
    result = {"retry_at": retry_at, "next_check_at": max(next_check, retry_at or 0) if next_check else None,
              "active_searches": len(active), "failed_searches": sum(bool(w.error) for w in active),
              "error": None, "browser": state.browser if state else None}
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
    elif state and state.checking_watch_id in {watch.id for watch in active}:
        code = "checking"
    elif all(watch.error for watch in active):
        code = "retrying"
    else:
        code = "monitoring"
    return result | {"state": code}
