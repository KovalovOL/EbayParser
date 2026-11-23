import httpx
import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Notification
from app.telegram import TelegramClient, TelegramError
from app.worker import deliver_notifications, scan_watch
from test_monitor import FakeEbay, listing, make_watch


async def test_telegram_rate_limit_exposes_full_retry_after():
    telegram = TelegramClient(token='test-token')
    await telegram.client.aclose()
    telegram.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
        httpx.Response(429, json={'ok': False, 'error_code': 429, 'parameters': {'retry_after': 1200}})))
    try:
        with pytest.raises(TelegramError) as error:
            await telegram.send(1, 'test')
        assert error.value.retry_after == 1200
    finally:
        await telegram.close()


async def test_delivery_honors_telegram_rate_limit_and_caps_network_backoff(user, monkeypatch):
    wid = make_watch(user, initialized=True)
    await scan_watch(wid, FakeEbay([listing()]))
    monkeypatch.setattr('app.worker.stamp', lambda: 1000)
    async def no_sleep(*args):
        pass
    monkeypatch.setattr('app.worker.asyncio.sleep', no_sleep)
    with SessionLocal() as db:
        note = db.scalar(select(Notification))
        note.next_attempt_at, note.attempts = 0, 30
        db.commit()

    class Telegram:
        retry_after = None
        async def send(self, chat_id, message):
            raise TelegramError('Unavailable', retry_after=self.retry_after)

    telegram = Telegram()
    await deliver_notifications(telegram)
    with SessionLocal() as db:
        note = db.scalar(select(Notification))
        assert note.next_attempt_at == 1300
        note.next_attempt_at = 0
        db.commit()
    telegram.retry_after = 1200
    await deliver_notifications(telegram)
    with SessionLocal() as db:
        assert db.scalar(select(Notification)).next_attempt_at == 2200
