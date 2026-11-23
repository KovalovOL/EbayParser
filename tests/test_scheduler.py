import asyncio
from contextlib import suppress
from types import SimpleNamespace

from app.db import SessionLocal
from app.models import MonitorState, Notification, Watch
from app.monitoring import record_result, restore_monitor
from app.parser.ebay import EbayAccessError
from app.worker import next_due_watch, notification_loop, scan_watch
from test_monitor import FakeEbay, listing, make_watch
from test_monitoring import saved_watch


async def test_repeated_blocks_rotate_through_overdue_searches(user, monkeypatch):
    clock = SimpleNamespace(now=1000)
    monkeypatch.setattr('app.worker.stamp', lambda: clock.now)
    monkeypatch.setattr('app.monitoring.stamp', lambda: clock.now)
    first = saved_watch(user, next_check_at=100)
    second = saved_watch(user, next_check_at=200)

    class Restricted:
        blocked_until = 0
        async def search(self, query, filters):
            self.blocked_until = clock.now + 900
            raise EbayAccessError('HTTP 403', self.blocked_until)

    client = Restricted()
    assert next_due_watch({'ebay': client}) == first
    await scan_watch(first, client, due_only=True)
    assert next_due_watch({'ebay': client}) is None
    clock.now = client.blocked_until
    assert next_due_watch({'ebay': client}) == second
    await scan_watch(second, client, due_only=True)
    clock.now = client.blocked_until
    assert next_due_watch({'ebay': client}) == first


async def test_blocked_ebay_does_not_delay_other_platform_or_count_as_attempt(user, monkeypatch):
    monkeypatch.setattr('app.worker.stamp', lambda: 1000)
    wid = saved_watch(user, platforms=['ebay', 'kleinanzeigen'], next_check_at=100,
                      platform_state={'ebay': {'next_check_at': 100}, 'kleinanzeigen': {'next_check_at': 100}})
    ebay = SimpleNamespace(blocked_until=1900)
    markets = {'kleinanzeigen': FakeEbay([])}
    assert next_due_watch({'ebay': ebay, **markets}) == wid
    await scan_watch(wid, ebay, markets, due_only=True)
    with SessionLocal() as db:
        states = db.get(Watch, wid).platform_state
        assert states['ebay'] == {'next_check_at': 100}
        assert states['kleinanzeigen']['last_success_at'] == 1000
    assert next_due_watch({'ebay': ebay, **markets}) is None


def test_restart_recovers_legacy_searches_displaced_by_global_pause(user, monkeypatch):
    monkeypatch.setattr('app.monitoring.stamp', lambda: 1000)
    wid = saved_watch(user, last_checked_at=100, next_check_at=1900, interval_seconds=300)
    record_result(1900, 'HTTP 403')
    restore_monitor(SimpleNamespace(blocked_until=0))
    with SessionLocal() as db:
        assert db.get(Watch, wid).next_check_at == 400
        assert db.get(MonitorState, 1).retry_at == 1900


async def test_notifications_are_delivered_while_a_scan_is_waiting(user):
    wid = make_watch(user, initialized=True)
    await scan_watch(wid, FakeEbay([listing()]))
    waiting, delivered = asyncio.Event(), asyncio.Event()
    gate = asyncio.Event()

    class SlowSearch:
        async def search(self, query, filters):
            waiting.set()
            await gate.wait()
            return [], False, 'https://www.ebay.com/sch/i.html'

    class Telegram:
        async def send(self, chat_id, message):
            delivered.set()

    scan = asyncio.create_task(scan_watch(wid, SlowSearch()))
    await asyncio.wait_for(waiting.wait(), timeout=1)
    delivery = asyncio.create_task(notification_loop(Telegram()))
    try:
        await asyncio.wait_for(delivered.wait(), timeout=1)
        assert not scan.done()
        with SessionLocal() as db:
            assert db.query(Notification).one().sent_at is not None
    finally:
        for task in (scan, delivery):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


async def test_unexpected_source_error_does_not_prevent_other_platform(user):
    wid = saved_watch(user, platforms=['ebay', 'kleinanzeigen'])
    class Broken:
        async def search(self, query, filters):
            raise RuntimeError('unexpected transport failure')
    await scan_watch(wid, Broken(), {'kleinanzeigen': FakeEbay([])})
    with SessionLocal() as db:
        states = db.get(Watch, wid).platform_state
        assert states['ebay']['failures'] == 1
        assert states['kleinanzeigen']['initialized'] is True


async def test_failed_scan_preserves_last_success_time(user):
    wid = make_watch(user)
    await scan_watch(wid, FakeEbay([]))
    with SessionLocal() as db:
        success = db.get(Watch, wid).platform_state['ebay']['last_success_at']
    await scan_watch(wid, FakeEbay(error=True))
    with SessionLocal() as db:
        state = db.get(Watch, wid).platform_state['ebay']
        assert state['last_success_at'] == success
        assert state['last_checked_at'] >= success


async def test_browser_restart_is_not_delayed_for_an_hour_by_old_access_failures(user, monkeypatch):
    from app.parser.errors import BrowserError
    monkeypatch.setattr('app.worker.stamp', lambda: 1000)
    wid = saved_watch(user, initialized=True, failures=900, interval_seconds=3600)
    class Crashed:
        async def search(self, query, filters):
            raise BrowserError('Browser could not start')
    await scan_watch(wid, Crashed())
    with SessionLocal() as db:
        assert db.get(Watch, wid).next_check_at == 1300
