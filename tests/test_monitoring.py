import time
from types import SimpleNamespace

import pytest

from app.db import SessionLocal
from app.healthcheck import healthy
from app.models import MonitorState, ServiceState, User, Watch
from app.monitoring import checking, record_browser, record_result, restore_monitor
from app.parser.ebay import EbayAccessError
from app.runtime import heartbeat
from app.worker import scan_watch


def saved_watch(user, **values):
    with SessionLocal() as db:
        watch = Watch(user_id=user.id, name="Sony", query="sony", **values)
        db.add(watch)
        db.commit()
        return watch.id


def status(client, auth):
    return client.get("/api/status", headers=auth).json()["monitoring"]


def test_liveness_is_separate_from_ebay_restrictions(client, auth, user):
    saved_watch(user)
    heartbeat("worker")
    heartbeat("bot")
    deadline = time.time() + 900
    record_result(deadline, "eBay restricted access (HTTP 403).")
    result = status(client, auth)
    assert healthy("worker") and healthy("bot")
    assert result["state"] == "restricted"
    assert result["retry_at"] == deadline and result["next_check_at"] >= deadline
    assert result["error"].startswith("eBay restricted")
    # Periodic process heartbeats must not clear a shared eBay pause.
    heartbeat("worker")
    assert status(client, auth)["state"] == "restricted"


def test_no_searches_paused_offline_and_error_states(client, auth, user):
    assert status(client, auth)["state"] == "offline"
    heartbeat("worker")
    assert status(client, auth)["state"] == "no_searches"
    watch_id = saved_watch(user, active=False)
    assert status(client, auth)["state"] == "paused"
    with SessionLocal() as db:
        db.get(Watch, watch_id).active = True
        db.commit()
    assert status(client, auth)["state"] == "monitoring"
    heartbeat("worker", "Monitoring encountered an internal error.")
    assert status(client, auth)["state"] == "error"
    with SessionLocal() as db:
        db.get(ServiceState, "worker").heartbeat_at = time.time() - 121
        db.commit()
    assert status(client, auth)["state"] == "offline"
    assert not healthy("worker")


def test_expired_restriction_does_not_leave_status_paused(client, auth, user):
    saved_watch(user, error="Could not connect to eBay.", next_check_at=time.time() + 100)
    heartbeat("worker")
    record_result(time.time() - 1, "Old HTTP 403")
    result = status(client, auth)
    assert result["state"] == "retrying" and result["retry_at"] is None
    assert result["failed_searches"] == 1


def test_current_check_is_scoped_to_user_and_cleared_on_exception(client, auth, user):
    own_id = saved_watch(user)
    heartbeat("worker")
    with SessionLocal() as db:
        other = User(telegram_id=789, chat_id=789, username="other_user")
        db.add(other)
        db.commit()
    other_id = saved_watch(other)
    with checking(other_id):
        assert status(client, auth)["state"] == "monitoring"
    with pytest.raises(RuntimeError), checking(own_id):
        assert status(client, auth)["state"] == "checking"
        raise RuntimeError("Interrupted scan")
    assert status(client, auth)["state"] == "monitoring"


async def test_block_is_persisted_and_restored_before_next_request(user):
    watch_id = saved_watch(user)
    deadline = time.time() + 900

    class RestrictedEbay:
        async def search(self, query, filters):
            raise EbayAccessError("HTTP 403", deadline)

    await scan_watch(watch_id, RestrictedEbay())
    restarted = SimpleNamespace(blocked_until=0)
    restore_monitor(restarted)
    assert restarted.blocked_until == deadline
    with SessionLocal() as db:
        assert db.get(MonitorState, 1).checking_watch_id is None
        assert not db.get(Watch, watch_id).initialized

    class WorkingEbay:
        async def search(self, query, filters):
            return [], False, "https://www.ebay.com/sch/i.html?_nkw=sony"

    await scan_watch(watch_id, WorkingEbay())
    restore_monitor(restarted)
    assert restarted.blocked_until == 0
    with SessionLocal() as db:
        assert db.get(MonitorState, 1).error is None
        assert db.get(Watch, watch_id).initialized


async def test_access_errors_do_not_add_per_watch_exponential_delays(user):
    first = saved_watch(user, failures=20, error="HTTP 403", next_check_at=time.time()+3600)
    second = saved_watch(user, failures=3, error="HTTP 403", next_check_at=time.time()+2400)
    paused = saved_watch(user, active=False, error="HTTP 403", next_check_at=time.time()+7200)
    healthy_later = saved_watch(user, interval_seconds=7200, next_check_at=time.time()+7200)
    deadline = time.time()+900

    class Blocked:
        async def search(self, query, filters):
            raise EbayAccessError("Chromium and Firefox returned HTTP 403", deadline)

    await scan_watch(first, Blocked())
    with SessionLocal() as db:
        assert db.get(Watch, first).next_check_at == db.get(Watch, second).next_check_at == deadline
        assert db.get(Watch, paused).next_check_at > deadline+6000
        assert db.get(Watch, healthy_later).next_check_at > deadline+6000


def test_restart_repairs_legacy_hour_delay_and_retains_shared_deadline(user):
    deadline = time.time()+900
    record_result(deadline, "HTTP 403")
    watch_id = saved_watch(user, error="HTTP 403", failures=10, next_check_at=time.time()+3600)
    restore_monitor(SimpleNamespace(blocked_until=0))
    with SessionLocal() as db:
        assert db.get(Watch, watch_id).next_check_at == deadline


def test_current_browser_is_visible_in_status(client, auth, user):
    watch_id = saved_watch(user)
    heartbeat("worker")
    with checking(watch_id):
        record_browser("chromium")
        assert status(client, auth)["browser"] == "chromium"
        record_browser("firefox")
        result = status(client, auth)
        assert result["state"] == "checking" and result["browser"] == "firefox"


@pytest.mark.parametrize("had_shared_state", [False, True])
def test_legacy_hour_backoff_is_repaired_even_after_another_search_cleared_pause(user, had_shared_state):
    if had_shared_state:
        record_result()  # A different search succeeded and cleared the shared deadline.
    failed = saved_watch(user, error="eBay restricted access (HTTP 403).", failures=3,
                         next_check_at=time.time()+2400)
    healthy = saved_watch(user, next_check_at=time.time()+300)
    restore_monitor(SimpleNamespace(blocked_until=0))
    with SessionLocal() as db:
        assert db.get(Watch, failed).next_check_at <= time.time()+1
        assert db.get(Watch, healthy).next_check_at > time.time()+295
