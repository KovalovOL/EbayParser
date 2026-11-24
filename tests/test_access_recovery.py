from email.utils import formatdate
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import time

import pytest

from app.config import Settings
from app.db import SessionLocal
from app.models import ServiceState, Watch
from app.monitoring import record_result, restore_access_state, restore_monitor
from app.parser.access import access_retry_at
from app.parser.markets import MarketAccessError, MarketClient
from app.worker import scan_watch


def test_recovery_delay_grows_only_after_repeated_denials_and_is_bounded(monkeypatch):
    monkeypatch.setattr("app.parser.access.time.time", lambda: 1000)
    assert [access_retry_at(900, n) - 1000 for n in range(1, 8)] == [900, 1800, 3600, 7200, 14400, 14400, 14400]
    assert access_retry_at(86400, 1000000) == 87400


@pytest.mark.parametrize("header, delay", [("21600", 21600), ("9999999", 86400),
                                           ("NaN", 900), ("inf", 900), ("-15", 900), ("bad", 900)])
def test_server_retry_after_never_shortens_recovery_or_breaks_scheduling(monkeypatch, header, delay):
    monkeypatch.setattr("app.parser.access.time.time", lambda: 1000)
    assert access_retry_at(900, 1, header) == 1000 + delay
    assert access_retry_at(900, 1, formatdate(1000 + 21600, usegmt=True)) == 22600


async def test_buyee_recovery_is_shared_across_searches_and_resets_on_real_success(monkeypatch):
    client = MarketClient(Settings(_env_file=None, request_delay_seconds=0, request_delay_jitter_seconds=0), "buyee")
    denied = SimpleNamespace(status_code=403, headers={})
    html = (Path(__file__).parent / "fixtures/buyee-empty.html").read_text()
    success = SimpleNamespace(status_code=200, text=html, content=html.encode(), headers={})
    get = AsyncMock(return_value=denied)
    monkeypatch.setattr(client.client, "get", get)
    try:
        for delay in (900, 1800, 3600):
            client.blocked_until = 0
            with pytest.raises(MarketAccessError) as failure:
                await client.search("sony", {})
            assert delay - 1 <= failure.value.retry_at - time.time() <= delay
            with pytest.raises(MarketAccessError):
                await client.search("different search", {})
        assert get.await_count == 3 and client.access_failures == 3
        client.blocked_until = 0
        get.return_value = success
        await client.search("sony", {})
        assert client.access_failures == client.blocked_until == 0
        get.return_value = denied
        with pytest.raises(MarketAccessError) as failure:
            await client.search("sony", {})
        assert 899 <= failure.value.retry_at - time.time() <= 900
    finally:
        await client.close()


def test_restart_preserves_ebay_recovery_and_working_browser():
    client = SimpleNamespace(access_failures=4, preferred_browser="firefox")
    deadline = time.time() + 7200
    record_result(deadline, "HTTP 403", client=client)
    restarted = SimpleNamespace(blocked_until=0, access_failures=0, preferred_browser="chromium")
    restore_monitor(restarted)
    assert (restarted.blocked_until, restarted.access_failures, restarted.preferred_browser) == (deadline, 4, "firefox")
    restarted.access_failures = 0
    record_result(client=restarted)
    restore_monitor(client)
    assert client.blocked_until == client.access_failures == 0
    assert client.preferred_browser == "firefox"


async def test_worker_persists_market_recovery_and_success_without_affecting_other_sources(user, monkeypatch):
    with SessionLocal() as db:
        watch = Watch(user_id=user.id, name="Sony", query="sony", platforms=["buyee"])
        db.add(watch)
        db.commit()
        watch_id = watch.id
    client = MarketClient(Settings(_env_file=None, request_delay_seconds=0, request_delay_jitter_seconds=0), "buyee")
    monkeypatch.setattr(client.client, "get", AsyncMock(return_value=SimpleNamespace(status_code=403, headers={})))
    try:
        for expected in (1, 2):
            client.blocked_until = 0
            await scan_watch(watch_id, None, {"buyee": client})
            restarted = SimpleNamespace(blocked_until=0, access_failures=0)
            with SessionLocal() as db:
                restore_access_state(db, "buyee", restarted)
                assert db.get(ServiceState, "access:kleinanzeigen") is None
            assert restarted.access_failures == expected
            assert restarted.blocked_until >= client.blocked_until
        html = (Path(__file__).parent / "fixtures/buyee-empty.html").read_text()
        client.blocked_until = 0
        client.client.get.return_value = SimpleNamespace(status_code=200, headers={}, text=html, content=html.encode())
        await scan_watch(watch_id, None, {"buyee": client})
        with SessionLocal() as db:
            assert db.get(ServiceState, "access:buyee").value == 0
            assert db.get(ServiceState, "access_failures:buyee").value == 0
    finally:
        await client.close()
