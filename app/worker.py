import asyncio
import logging
import math
import signal
import time
from contextlib import suppress

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert

from app.config import get_settings
from app.db import init_db, session_scope, stamp
from app.models import LoginChallenge, Notification, RefreshSession, SeenListing, ServiceState, User, Watch, WatchItem
from app.listing_identity import listing_identity
from app.monitoring import checking, platform_next_check, record_browser, record_result, restore_monitor
from app.parser.ebay import EbayAccessError, EbayClient, Listing, ParseError, matches_filters
from app.parser.errors import BrowserError
from app.parser.markets import MarketAccessError, MarketClient, matches_market
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


def matches_source(listing, filters):
    return matches_filters(listing, filters, native=True) if listing.platform == "ebay" else matches_market(listing, filters)


def save_platform_state(watch, platform, result):
    watch.platform_state = {**watch.platform_state, platform: result}
    if len(watch.platforms) == 1:
        watch.initialized = result.get("initialized", False)
        watch.search_url = result.get("search_url")
        watch.next_check_at = result["next_check_at"]
    else:
        states = [watch.platform_state.get(key, {}) for key in watch.platforms]
        watch.initialized = all(entry.get("initialized") for entry in states)
        watch.search_url = watch.platform_state.get("ebay", {}).get("search_url")
        watch.next_check_at = min(entry.get("next_check_at", stamp()) for entry in states)
    states = [watch.platform_state.get(key, {}) for key in watch.platforms]
    watch.truncated = any(entry.get("truncated") for entry in states)
    watch.error = " · ".join(entry["error"] for entry in states if entry.get("error")) or None
    watch.failures = max((entry.get("failures", 0) for entry in states), default=0)
    watch.last_checked_at = stamp()


async def scan_watch(watch_id: int, ebay: EbayClient, markets=None, *, due_only=False):
    with session_scope() as db:
        watch = db.get(Watch, watch_id)
        if not watch or not watch.active:
            return
        platforms, revision = tuple(watch.platforms), watch.revision
    clients = {"ebay": ebay, **(markets or {})}
    for platform in platforms:
        await scan_platform(watch_id, revision, platform, clients[platform], due_only=due_only)


async def scan_platform(watch_id, revision, platform, client, *, due_only):
    with session_scope() as db:
        watch = db.get(Watch, watch_id)
        if not watch or not watch.active or watch.revision != revision:
            return
        old = dict(watch.platform_state.get(platform, {}))
        if due_only and max(platform_next_check(watch, platform), getattr(client, "blocked_until", 0)) > stamp():
            return
        # The older database stored the eBay baseline directly on the watch.
        old.setdefault("initialized", watch.initialized if platform == "ebay" else False)
        old.setdefault("failures", watch.failures if platform == "ebay" and len(watch.platforms) == 1 else 0)
        if platform == "ebay" and watch.search_url:
            old.setdefault("search_url", watch.search_url)
        query = watch.query
        filters = dict(watch.filters if platform == "ebay" else watch.market_filters.get(platform, {}))
    try:
        with checking(watch_id, platform):
            try:
                async with asyncio.timeout(180):
                    listings, truncated, url = await client.search(query, filters)
            except TimeoutError:
                raise ParseError("Source check timed out. A retry is scheduled automatically.") from None
            except ParseError:
                raise
            except Exception as exc:
                log.error("Search %s / %s raised %s", watch_id, platform, type(exc).__name__)
                raise ParseError("Source check failed unexpectedly. A retry is scheduled automatically.") from None
    except ParseError as exc:
        log.warning("Search %s / %s failed: %s", watch_id, platform, exc)
        if isinstance(exc, EbayAccessError):
            record_result(exc.retry_at, str(exc))
        elif isinstance(exc, MarketAccessError):
            with session_scope() as db:
                key = f"access:{platform}"
                row = db.get(ServiceState, key)
                if row is None:
                    row = ServiceState(name=key)
                    db.add(row)
                row.value = math.ceil(exc.retry_at)
        with session_scope() as db:
            watch = db.get(Watch, watch_id)
            if watch and watch.revision == revision and watch.active:
                failures = old["failures"] + 1
                if isinstance(exc, (EbayAccessError, MarketAccessError)):
                    retry = exc.retry_at
                elif isinstance(exc, BrowserError):
                    retry = stamp() + min(300, 30 * 2 ** min(failures, 4))
                else:
                    retry = stamp() + min(3600, watch.interval_seconds * 2 ** min(failures, 5))
                save_platform_state(watch, platform, old | {
                    "error": str(exc), "failures": failures, "next_check_at": retry,
                    "last_checked_at": stamp()})
        return
    if platform == "ebay":
        record_result()
    with session_scope() as db:
        watch = db.get(Watch, watch_id)
        if not watch or watch.revision != revision or not watch.active:
            return
        baseline = not old["initialized"]
        queued = 0
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
            item.matches = matches_source(listing, filters)
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
                # Freeze each price/location event. Later edits to this listing must not
                # change an already queued Telegram message or prevent another new identity.
                db.add(Notification(item_id=item.id, seen_listing_id=seen_id, data=data))
                queued += 1
        save_platform_state(watch, platform, old | {
            "initialized": True, "error": None, "failures": 0, "truncated": truncated,
            "search_url": url, "next_check_at": stamp() + watch.interval_seconds,
            "last_checked_at": stamp(), "last_success_at": stamp()})
        log.info("Search %s / %s: %s listings, %s alerts queued, baseline=%s",
                 watch_id, platform, len(listings), queued, baseline)


async def deliver_notifications(telegram: TelegramClient):
    with session_scope() as db:
        ids = list(db.scalars(select(Notification.id).where(Notification.sent_at.is_(None),
            Notification.next_attempt_at <= stamp()).order_by(Notification.id).limit(20)))
    for notification_id in ids:
        heartbeat("delivery")
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
            platform = data.get("platform", "ebay")
            filters = watch.filters if platform == "ebay" else watch.market_filters.get(platform, {})
            if platform not in watch.platforms or matches_source(Listing(**data), filters) is not True:
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
            chat_id, message = user.chat_id, item_message(watch.name, data)
        # Never keep a database transaction open during network I/O: scans and
        # delivery run independently and must not hold up each other's writes.
        failure = None
        try:
            await telegram.send(chat_id, message)
        except TelegramError as exc:
            failure = exc
        with session_scope() as db:
            notification = db.get(Notification, notification_id)
            if notification is None:
                continue
            if failure:
                notification.attempts += 1
                notification.error = str(failure)
                delay = max(failure.retry_after or 0, min(300, 15 * 2 ** min(notification.attempts, 5)))
                notification.next_attempt_at = stamp() + delay
                log.warning("Notification %s failed; retry in %ss: %s", notification_id, delay, failure)
            else:
                notification.sent_at, notification.error = stamp(), None
                log.info("Notification %s delivered", notification_id)
        await asyncio.sleep(1.1)  # Below Telegram's per-chat free message rate.


def cleanup():
    with session_scope() as db:
        db.execute(delete(LoginChallenge).where(LoginChallenge.expires_at < stamp() - 86400))
        db.execute(delete(RefreshSession).where(RefreshSession.expires_at < stamp() - 86400))


async def keep_heartbeat():
    while True:
        await asyncio.sleep(20)
        heartbeat("worker")


def next_due_watch(clients):
    now = stamp()
    with session_scope() as db:
        due = [(platform_next_check(watch, platform), watch.id)
               for watch in db.scalars(select(Watch).where(Watch.active.is_(True), Watch.next_check_at <= now))
               for platform in watch.platforms
               if max(platform_next_check(watch, platform), getattr(clients[platform], "blocked_until", 0)) <= now]
        return min(due)[1] if due else None


async def notification_loop(telegram):
    while True:
        try:
            await deliver_notifications(telegram)
            heartbeat("delivery")
        except Exception:
            heartbeat("delivery", "Notification delivery failed. Check the worker logs.")
            log.exception("Notification delivery iteration failed")
        await asyncio.sleep(2)


async def run():
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
    init_db()
    lock = process_lock("worker")
    settings = get_settings()
    ebay, telegram = EbayClient(settings, on_browser=record_browser), TelegramClient()
    markets = {key: MarketClient(settings, key) for key in ("buyee", "kleinanzeigen")}
    restore_monitor(ebay)
    with session_scope() as db:
        for platform, client in markets.items():
            if state := db.get(ServiceState, f"access:{platform}"):
                client.blocked_until = state.value
    log.info("One results page per selected platform; no item detail requests. Request delay: %.1f-%.1f seconds",
             settings.request_delay_seconds,
             settings.request_delay_seconds + settings.request_delay_jitter_seconds)
    last_cleanup = 0
    pulse = asyncio.create_task(keep_heartbeat())
    delivery = asyncio.create_task(notification_loop(telegram))
    try:
        while True:
            try:
                heartbeat("worker")
                watch_id = next_due_watch({"ebay": ebay, **markets})
                if watch_id:
                    await scan_watch(watch_id, ebay, markets, due_only=True)
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
        delivery.cancel()
        with suppress(asyncio.CancelledError):
            await pulse
        with suppress(asyncio.CancelledError):
            await delivery
        await ebay.close()
        for client in markets.values():
            await client.close()
        await telegram.close()
        lock.close()


if __name__ == "__main__":
    configure_logging()
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
