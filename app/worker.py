import asyncio
import logging
import signal
import time
from contextlib import suppress

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert

from app.config import get_settings
from app.db import init_db, session_scope, stamp
from app.models import LoginChallenge, Notification, RefreshSession, SeenListing, User, Watch, WatchItem
from app.listing_identity import listing_identity
from app.monitoring import checking, record_browser, record_result, restore_monitor
from app.parser.ebay import EbayAccessError, EbayClient, Listing, ParseError, matches_filters
from app.runtime import configure_logging, heartbeat, process_lock
from app.telegram import TelegramClient, TelegramError, item_message

log = logging.getLogger(__name__)


def merge_listing(old: dict, fresh: dict) -> dict:
    merged = dict(old)
    for key, value in fresh.items():
        if key in {"attributes", "raw"}:
            merged[key] = {**old.get(key, {}), **value}
        else:
            merged[key] = value
    return merged


async def scan_watch(watch_id: int, ebay: EbayClient):
    with session_scope() as db:
        watch = db.get(Watch, watch_id)
        if not watch or not watch.active:
            return
        revision, query, filters = watch.revision, watch.query, dict(watch.filters)
    try:
        # One recognized results page is the complete check; never fetch item pages.
        with checking(watch_id):
            listings, truncated, url = await ebay.search(query, filters)
    except ParseError as exc:
        if isinstance(exc, EbayAccessError):
            record_result(exc.retry_at, str(exc))
        with session_scope() as db:
            watch = db.get(Watch, watch_id)
            if watch and watch.revision == revision and watch.active:
                watch.failures += 1
                watch.error = str(exc)
                watch.last_checked_at = stamp()
                if isinstance(exc, EbayAccessError):
                    watch.next_check_at = exc.retry_at
                else:
                    watch.next_check_at = stamp() + min(3600, watch.interval_seconds * 2 ** min(watch.failures, 5))
        return
    record_result()
    with session_scope() as db:
        watch = db.get(Watch, watch_id)
        if not watch or watch.revision != revision or not watch.active:
            return
        baseline = not watch.initialized
        for listing in listings:
            data = listing.to_dict()
            identity = listing_identity(data)
            item = db.scalar(select(WatchItem).where(WatchItem.watch_id == watch_id, WatchItem.ebay_id == listing.ebay_id))
            previous_identity = listing_identity(item.data)["fingerprint"] if item else None
            if item is None:
                item = WatchItem(watch_id=watch_id, ebay_id=listing.ebay_id, data=data, baseline=baseline)
                db.add(item)
                db.flush()
            else:
                item.data = merge_listing(item.data, data)
            item.last_seen_at = stamp()
            item.matches = matches_filters(listing, filters, native=True)
            seen_id = db.scalar(insert(SeenListing).values(
                user_id=watch.user_id, first_seen_at=stamp(), **identity
            ).on_conflict_do_nothing(index_elements=["user_id", "fingerprint"]).returning(SeenListing.id))
            changed = previous_identity != identity["fingerprint"]
            if baseline:
                item.baseline, item.is_new = True, False
            elif changed:
                item.baseline = False
                item.is_new = seen_id is not None and item.matches is True
            elif item.matches is not True:
                item.is_new = False
            if not baseline and changed and seen_id is not None and item.matches is True:
                # Freeze each price/location event. Later edits to this eBay ID must not
                # change an already queued Telegram message or prevent another new identity.
                db.add(Notification(item_id=item.id, seen_listing_id=seen_id, data=data))
        watch.initialized = True
        watch.error, watch.failures = None, 0
        watch.truncated, watch.search_url = truncated, url
        watch.last_checked_at, watch.next_check_at = stamp(), stamp() + watch.interval_seconds


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
            if not item or not watch or not watch.active:
                db.delete(notification)
                continue
            data = notification.data or item.data
            if matches_filters(Listing(**data), watch.filters, native=True) is not True:
                db.delete(notification)
                continue
            if notification.seen_listing_id is None:
                # Pending duplicates inherited from the previous per-search outbox.
                seen_id = db.scalar(select(SeenListing.id).where(
                    SeenListing.user_id == watch.user_id,
                    SeenListing.fingerprint == listing_identity(data)["fingerprint"]))
                if seen_id is not None and db.scalar(select(Notification.id).where(
                    Notification.seen_listing_id == seen_id, Notification.id != notification.id)):
                    db.delete(notification)
                    continue
            user = db.get(User, watch.user_id)
            try:
                await telegram.send(user.chat_id, item_message(watch.name, data))
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
    settings = get_settings()
    ebay, telegram = EbayClient(settings, on_browser=record_browser), TelegramClient()
    restore_monitor(ebay)
    log.info("One eBay results page per check; no item detail requests. Request delay: %.1f-%.1f seconds",
             settings.request_delay_seconds,
             settings.request_delay_seconds + settings.request_delay_jitter_seconds)
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
                if watch_id and ebay.blocked_until <= stamp():
                    await scan_watch(watch_id, ebay)
                    await deliver_notifications(telegram)
                else:
                    await asyncio.sleep(3)
                if time.monotonic() - last_cleanup > 3600:
                    cleanup()
                    last_cleanup = time.monotonic()
            except Exception:
                heartbeat("worker", "Monitoring encountered an internal error. Check the worker logs.")
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
