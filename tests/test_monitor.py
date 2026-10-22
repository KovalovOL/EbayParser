import time

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import Notification, SeenListing, User, Watch, WatchItem
from app.parser.ebay import EbayAccessError, Listing, ParseError
from app.schemas import Filters
from app.telegram import TelegramError
from app.worker import deliver_notifications, scan_watch


class FakeEbay:
    def __init__(self, listings=None, error=False):
        self.listings = listings or []
        self.error = error

    async def search(self, query, filters):
        if self.error: raise ParseError("HTTP 403")
        return self.listings, False, "https://www.ebay.com/sch/i.html?_nkw=sony"

    async def detail(self, listing):
        raise AssertionError("Monitoring must never request an item detail page")


def make_watch(user, initialized=False):
    with SessionLocal() as db:
        watch = Watch(user_id=user.id, name="Sony", query="sony", filters=Filters().model_dump(),
                      tracking_since=time.time()-100,initialized=initialized)
        db.add(watch); db.commit()
        return watch.id


def listing(id="123456789001", published=None):
    return Listing(id, f"https://www.ebay.com/itm/{id}", "Sony", price=100, currency="USD", published_at=published)


def count_notifications():
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(Notification))


async def test_initial_snapshot_never_notifies_even_with_recent_date(user):
    wid = make_watch(user)
    ebay = FakeEbay([listing(published=time.time()-5)])
    await scan_watch(wid,ebay)
    await scan_watch(wid,ebay)
    assert count_notifications() == 0
    with SessionLocal() as db:
        assert db.get(Watch,wid).initialized
        item = db.scalar(select(WatchItem))
        assert item.baseline and item.matches
        assert not item.is_new


async def test_publication_date_does_not_control_new_composite_notifications(user):
    wid = make_watch(user,initialized=True)
    await scan_watch(wid,FakeEbay([listing(published=time.time()-86400*30)]))
    assert count_notifications() == 1


async def test_new_composite_without_publication_date_notifies(user):
    wid = make_watch(user,initialized=True)
    await scan_watch(wid,FakeEbay([listing()]))
    assert count_notifications() == 1
    with SessionLocal() as db:
        assert db.scalar(select(WatchItem)).matches


async def test_new_listing_enqueued_once_across_restarts(user):
    wid = make_watch(user,initialized=True)
    ebay = FakeEbay([listing(published=time.time()-5)])
    await scan_watch(wid,ebay)
    await scan_watch(wid,ebay)
    assert count_notifications() == 1


async def test_failed_baseline_remains_uninitialized_and_backs_off(user):
    wid = make_watch(user)
    await scan_watch(wid,FakeEbay(error=True))
    with SessionLocal() as db:
        watch = db.get(Watch,wid)
        assert not watch.initialized
        assert watch.failures == 1
        assert watch.error == "HTTP 403"
        assert watch.next_check_at > time.time()+500
    await scan_watch(wid,FakeEbay([listing()]))
    assert count_notifications() == 0


async def test_shared_access_cooldown_is_persisted_in_watch(user):
    wid = make_watch(user)
    retry_at = time.time() + 7200
    class BlockedEbay(FakeEbay):
        async def search(self, query, filters):
            raise EbayAccessError("eBay requires a pause", retry_at)
    await scan_watch(wid, BlockedEbay())
    with SessionLocal() as db:
        assert db.get(Watch, wid).next_check_at >= retry_at
        assert not db.get(Watch, wid).initialized


async def test_old_detail_errors_never_trigger_detail_requests(user):
    wid = make_watch(user, initialized=True)
    with SessionLocal() as db:
        db.add(WatchItem(watch_id=wid, ebay_id="123456789001", data=listing().to_dict(), baseline=True,
                         matches=True, detailed_at=time.time() - 86400, detail_error="HTTP 403"))
        db.commit()
    ebay = FakeEbay([listing()])
    await scan_watch(wid, ebay)
    assert count_notifications() == 0


async def test_filtered_out_does_not_notify(user):
    wid = make_watch(user,initialized=True)
    with SessionLocal() as db:
        db.get(Watch,wid).filters = Filters(max_price=50).model_dump(); db.commit()
    await scan_watch(wid,FakeEbay([listing(published=time.time()-5)]))
    assert count_notifications() == 0


async def test_revision_change_discards_inflight_scan(user):
    wid = make_watch(user)
    class EditingEbay(FakeEbay):
        async def search(self,query,filters):
            with SessionLocal() as db:
                db.get(Watch,wid).revision += 1; db.commit()
            return await super().search(query,filters)
    await scan_watch(wid,EditingEbay([listing()]))
    with SessionLocal() as db:
        assert not db.get(Watch,wid).initialized
        assert db.scalar(select(func.count()).select_from(WatchItem)) == 0


async def test_delivery_retries_and_then_marks_sent(user, monkeypatch):
    wid = make_watch(user,initialized=True)
    await scan_watch(wid,FakeEbay([listing(published=time.time()-5)]))
    class Telegram:
        failing=True
        messages=[]
        async def send(self,chat_id,message):
            if self.failing: raise TelegramError("temporarily unavailable")
            self.messages.append((chat_id,message))
    tg=Telegram()
    async def no_sleep(*args): pass
    monkeypatch.setattr("app.worker.asyncio.sleep", no_sleep)
    await deliver_notifications(tg)
    with SessionLocal() as db:
        note=db.scalar(select(Notification))
        assert note.attempts == 1 and note.sent_at is None
        note.next_attempt_at = 0; db.commit()
    tg.failing=False
    await deliver_notifications(tg)
    await deliver_notifications(tg)
    assert len(tg.messages) == 1
    assert tg.messages[0][0] == user.chat_id


async def test_paused_watch_cancels_queued_delivery(user, monkeypatch):
    wid=make_watch(user,initialized=True)
    await scan_watch(wid,FakeEbay([listing(published=time.time()-5)]))
    with SessionLocal() as db:
        db.get(Watch,wid).active=False; db.commit()
    class Telegram:
        async def send(self,*args): raise AssertionError("must not deliver")
    await deliver_notifications(Telegram())
    assert count_notifications() == 0


async def test_all_search_cards_are_saved_without_item_requests(user):
    wid = make_watch(user)
    cards = [listing(id=str(123456789001 + number)) for number in range(56)]
    for number, card in enumerate(cards):
        card.title = f"Product {number}"
    ebay = FakeEbay(cards)
    await scan_watch(wid, ebay)
    with SessionLocal() as db:
        assert db.get(Watch, wid).initialized
        assert db.scalar(select(func.count()).select_from(WatchItem)) == 56
        assert db.scalar(select(func.count()).select_from(SeenListing)) == 56
    await scan_watch(wid, ebay)
    assert count_notifications() == 0


async def test_price_location_and_title_changes_notify_once_per_combination(user):
    wid = make_watch(user)
    card = listing()
    card.location = "Japan"
    ebay = FakeEbay([card])
    await scan_watch(wid, ebay)  # Initial price 100: no message.
    card.price = 90
    await scan_watch(wid, ebay)
    card.price = 80
    await scan_watch(wid, ebay)
    card.price = 90
    await scan_watch(wid, ebay)  # Previously seen price: no repeat.
    assert count_notifications() == 2
    card.location = "Canada"
    await scan_watch(wid, ebay)
    card.title = "Sony updated title"
    await scan_watch(wid, ebay)
    assert count_notifications() == 4
    with SessionLocal() as db:
        alerts = list(db.scalars(select(Notification).order_by(Notification.id)))
        assert alerts[0].data["price"] == 90 and alerts[0].data["location"] == "Japan"
        assert alerts[1].data["price"] == 80
        assert db.scalar(select(func.count()).select_from(WatchItem)) == 1
        assert db.scalar(select(func.count()).select_from(SeenListing)) == 5


async def test_new_ebay_id_with_same_composite_is_not_new(user):
    wid = make_watch(user)
    await scan_watch(wid, FakeEbay([listing()]))
    await scan_watch(wid, FakeEbay([listing(id="123456789999")]))
    assert count_notifications() == 0
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(SeenListing)) == 1


async def test_composites_are_deduplicated_across_searches_but_not_accounts(user):
    first, second = make_watch(user), make_watch(user)
    await scan_watch(first, FakeEbay([listing()]))
    await scan_watch(second, FakeEbay([listing()]))
    fresh = listing(id="123456789002")
    fresh.title = "A different product"
    await scan_watch(first, FakeEbay([fresh]))
    await scan_watch(second, FakeEbay([fresh]))
    assert count_notifications() == 1
    with SessionLocal() as db:
        other = User(telegram_id=654321, chat_id=654321, username="other_user")
        db.add(other)
        db.commit()
    third = make_watch(other, initialized=True)
    await scan_watch(third, FakeEbay([fresh]))
    assert count_notifications() == 2


async def test_seen_history_survives_search_deletion(user):
    wid = make_watch(user)
    await scan_watch(wid, FakeEbay([listing()]))
    with SessionLocal() as db:
        db.delete(db.get(Watch, wid))
        db.commit()
        assert db.scalar(select(func.count()).select_from(SeenListing)) == 1
    wid = make_watch(user, initialized=True)
    await scan_watch(wid, FakeEbay([listing()]))
    assert count_notifications() == 0


async def test_queued_price_snapshots_survive_later_changes(user, monkeypatch):
    wid = make_watch(user)
    card = listing()
    ebay = FakeEbay([card])
    await scan_watch(wid, ebay)
    card.price = 90
    await scan_watch(wid, ebay)
    card.price = 80
    await scan_watch(wid, ebay)
    card.price = 100  # Back to the baseline; both earlier alerts are still valid.
    await scan_watch(wid, ebay)
    messages = []
    class Telegram:
        async def send(self, chat_id, message):
            messages.append(message)
    async def no_sleep(*args):
        pass
    monkeypatch.setattr("app.worker.asyncio.sleep", no_sleep)
    await deliver_notifications(Telegram())
    assert len(messages) == 2
    assert "90 USD" in messages[0] and "80 USD" in messages[1]
