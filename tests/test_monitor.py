import time

import pytest

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import Notification, Watch, WatchItem
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
        return listing


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


async def test_old_listing_appearing_on_later_page_does_not_notify(user):
    wid = make_watch(user,initialized=True)
    await scan_watch(wid,FakeEbay([listing(published=time.time()-86400*30)]))
    assert count_notifications() == 0


async def test_unknown_date_is_visible_without_notification(user):
    wid = make_watch(user,initialized=True)
    await scan_watch(wid,FakeEbay([listing()]))
    assert count_notifications() == 0
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


@pytest.mark.parametrize("age, expected_attempts", [(10, 0), (901, 1)])
async def test_failed_details_retry_after_cooldown_instead_of_a_day(user, age, expected_attempts):
    wid = make_watch(user, initialized=True)
    with SessionLocal() as db:
        db.add(WatchItem(watch_id=wid, ebay_id="123456789001", data=listing().to_dict(), baseline=True,
                         matches=True, detailed_at=time.time() - age, detail_error="HTTP 403"))
        db.commit()
    class RetryingEbay(FakeEbay):
        attempts = 0
        async def detail(self, item):
            self.attempts += 1
            return item
    ebay = RetryingEbay()
    await scan_watch(wid, ebay)
    assert ebay.attempts == expected_attempts


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
