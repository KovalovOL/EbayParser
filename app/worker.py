import asyncio
import logging
import signal
import time
from contextlib import suppress

from sqlalchemy import delete, or_, select

from app.config import get_settings
from app.db import init_db, session_scope, stamp
from app.models import LoginChallenge, Notification, RefreshSession, User, Watch, WatchItem
from app.parser.ebay import EbayAccessError, EbayClient, Listing, ParseError, matches_filters
from app.runtime import configure_logging, heartbeat, process_lock
from app.telegram import TelegramClient, TelegramError, item_message

log = logging.getLogger(__name__)


def enqueue_if_new(db, watch: Watch, item: WatchItem):
    published = item.data.get("published_at")
    item.is_new = bool(not item.baseline and item.matches is True and published
                       and watch.tracking_since < published <= stamp() + 60)
    if item.is_new and not db.scalar(select(Notification.id).where(Notification.item_id == item.id)):
        db.add(Notification(item_id=item.id))


def merge_listing(old: dict, fresh: dict) -> dict:
    merged = dict(old)
    for key, value in fresh.items():
        if key in {"attributes", "raw"}:
            merged[key] = {**old.get(key, {}), **value}
        elif value is not None and value != "":
            merged[key] = value
    return merged


async def scan_watch(watch_id: int, ebay: EbayClient):
    with session_scope() as db:
        watch = db.get(Watch, watch_id)
        if not watch or not watch.active:
            return
        revision, query, filters = watch.revision, watch.query, dict(watch.filters)
    try:
        # All configured pages must succeed before committing the baseline.
        listings, truncated, url = await ebay.search(query, filters)
    except ParseError as exc:
        with session_scope() as db:
            watch = db.get(Watch, watch_id)
            if watch and watch.revision == revision and watch.active:
                watch.failures += 1
                watch.error = str(exc)
                watch.last_checked_at = stamp()
                watch.next_check_at = stamp() + min(3600, watch.interval_seconds * 2 ** min(watch.failures, 5))
                if isinstance(exc, EbayAccessError):
                    watch.next_check_at = max(watch.next_check_at, exc.retry_at)
        return
    with session_scope() as db:
        watch = db.get(Watch, watch_id)
        if not watch or watch.revision != revision or not watch.active:
            return
        baseline = not watch.initialized
        for listing in listings:
            item = db.scalar(select(WatchItem).where(WatchItem.watch_id == watch_id, WatchItem.ebay_id == listing.ebay_id))
            if item is None:
                item = WatchItem(watch_id=watch_id, ebay_id=listing.ebay_id, data=listing.to_dict(), baseline=baseline)
                db.add(item)
                db.flush()
            else:
                item.data = merge_listing(item.data, listing.to_dict())
                if baseline:
                    item.baseline = True
            item.last_seen_at = stamp()
            item.matches = matches_filters(Listing(**item.data), filters)
            enqueue_if_new(db, watch, item)
        watch.initialized = True
        watch.error, watch.failures = None, 0
        watch.truncated, watch.search_url = truncated, url
        watch.last_checked_at, watch.next_check_at = stamp(), stamp() + watch.interval_seconds
    # Enrich a bounded batch. Missing timestamps never fall back to first_seen_at.
    with session_scope() as db:
        candidates = [(x.id, dict(x.data)) for x in db.scalars(select(WatchItem).where(
            WatchItem.watch_id == watch_id, or_(WatchItem.matches.is_(True), WatchItem.matches.is_(None)),
            or_(WatchItem.detailed_at.is_(None), WatchItem.detailed_at < stamp() - 86400,
                (WatchItem.detail_error.is_not(None)
                 & (WatchItem.detailed_at < stamp() - get_settings().ebay_block_cooldown_seconds)))
        ).order_by(WatchItem.detailed_at.asc(), WatchItem.baseline.asc(), WatchItem.id.desc())
            .limit(get_settings().details_per_scan))]
    for item_id, data in candidates:
        heartbeat("worker")
        error = None
        retry_at = None
        try:
            listing = await ebay.detail(Listing(**data))
        except ParseError as exc:
            listing, error = None, str(exc)
            if isinstance(exc, EbayAccessError):
                retry_at = exc.retry_at
        with session_scope() as db:
            watch = db.get(Watch, watch_id)
            item = db.get(WatchItem, item_id)
            if not watch or not item or watch.revision != revision or not watch.active:
                return
            item.detailed_at = stamp()
            item.detail_error = error
            if listing:
                item.data = listing.to_dict()
                item.matches = matches_filters(listing, filters)
                enqueue_if_new(db, watch, item)
            else:
                watch.error = "Пошук працює, але характеристики недоступні: " + error
                if retry_at:
                    watch.next_check_at = max(watch.next_check_at, retry_at)
        if error:
            # Avoid repeatedly hitting a blocked server for every item in this batch.
            break


async def deliver_notifications(telegram: TelegramClient):
    with session_scope() as db:
        ids = list(db.scalars(select(Notification.id).where(Notification.sent_at.is_(None),
            Notification.next_attempt_at <= stamp()).order_by(Notification.id).limit(20)))
    for notification_id in ids:
        with session_scope() as db:
            notification = db.get(Notification, notification_id)
            if not notification or notification.sent_at:
                continue
            item = db.get(WatchItem, notification.item_id)
            watch = db.get(Watch, item.watch_id) if item else None
            if not item or not watch or not watch.active or not item.is_new or item.matches is not True:
                db.delete(notification)
                continue
            user = db.get(User, watch.user_id)
            try:
                await telegram.send(user.chat_id, item_message(watch.name, item.data))
            except TelegramError as exc:
                notification.attempts += 1
                notification.error = str(exc)
                notification.next_attempt_at = stamp() + min(21600, 30 * 2 ** min(notification.attempts, 10))
            else:
                notification.sent_at, notification.error = stamp(), None
        heartbeat("worker")
        await asyncio.sleep(1.1)  # Below Telegram's per-chat free message rate.


def cleanup():
    with session_scope() as db:
        db.execute(delete(LoginChallenge).where(LoginChallenge.expires_at < stamp() - 86400))
        db.execute(delete(RefreshSession).where(RefreshSession.expires_at < stamp() - 86400))


async def keep_heartbeat():
    while True:
        await asyncio.sleep(20)
        heartbeat("worker")


async def run():
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
    init_db()
    lock = process_lock("worker")
    ebay, telegram = EbayClient(get_settings()), TelegramClient()
    last_cleanup = 0
    pulse = asyncio.create_task(keep_heartbeat())
    try:
        while True:
            try:
                heartbeat("worker")
                await deliver_notifications(telegram)
                with session_scope() as db:
                    watch_id = db.scalar(select(Watch.id).where(Watch.active.is_(True), Watch.next_check_at <= stamp())
                                         .order_by(Watch.next_check_at).limit(1))
                if watch_id:
                    await scan_watch(watch_id, ebay)
                    await deliver_notifications(telegram)
                else:
                    await asyncio.sleep(3)
                if time.monotonic() - last_cleanup > 3600:
                    cleanup()
                    last_cleanup = time.monotonic()
            except Exception:
                heartbeat("worker", "Внутрішня помилка моніторингу; дивіться логи worker")
                log.exception("Monitor iteration failed")
                await asyncio.sleep(10)
    finally:
        pulse.cancel()
        with suppress(asyncio.CancelledError):
            await pulse
        await ebay.close()
        await telegram.close()
        lock.close()


if __name__ == "__main__":
    configure_logging()
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
