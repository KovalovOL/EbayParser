import time
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Notification, Watch, WatchItem
from app.worker import scan_watch


def seeded_watch(client, auth, *, legacy=False):
    body = {
        "name": "Sony",
        "query": "sony",
        "filters": {"max_price": 100},
        "platforms": ["ebay"] if legacy else ["ebay", "buyee", "kleinanzeigen"],
        "market_filters": {}
        if legacy
        else {"buyee": {"max_price": 1000}, "kleinanzeigen": {"max_price": 100}},
    }
    saved = client.post("/api/watches", headers=auth, json=body).json()
    future = time.time() + 900
    with SessionLocal() as db:
        watch = db.get(Watch, saved["id"])
        watch.initialized = True
        watch.next_check_at = future
        watch.last_checked_at = 123
        watch.search_url = "https://www.ebay.com/sch/i.html?_nkw=sony"
        if not legacy:
            watch.platform_state = {
                p: {
                    "initialized": True,
                    "next_check_at": future,
                    "last_success_at": 123,
                    "failures": 0,
                    "error": None,
                    "search_url": p + "-results",
                }
                for p in watch.platforms
            }
        for n, p in enumerate(watch.platforms):
            # Older eBay cards do not have a platform field.
            data = (
                {"title": p, "price": 10, "currency": "USD"} if p == "ebay" else {"platform": p, "title": p}
            )
            item = WatchItem(
                watch_id=watch.id, ebay_id=str(123456789001 + n), data=data, baseline=False, matches=True
            )
            db.add(item)
            db.flush()
            db.add(Notification(item_id=item.id, data=data))
        db.commit()
        revision, tracking = watch.revision, watch.tracking_since
    return saved["id"], body, future, revision, tracking


def platforms_with_cards_and_alerts(wid):
    with SessionLocal() as db:
        cards = list(db.scalars(select(WatchItem).where(WatchItem.watch_id == wid)))
        alerts = list(db.scalars(select(Notification).join(WatchItem).where(WatchItem.watch_id == wid)))
        return (
            {item.data.get("platform", "ebay") for item in cards},
            {item.data.get("platform", "ebay") for item in alerts},
        )


async def test_adding_market_keeps_legacy_ebay_baseline_alerts_and_schedule(client, auth):
    wid, body, future, revision, tracking = seeded_watch(client, auth, legacy=True)
    body["platforms"] = ["ebay", "kleinanzeigen"]
    response = client.put(f"/api/watches/{wid}", headers=auth, json=body)
    assert response.status_code == 200
    assert platforms_with_cards_and_alerts(wid) == ({"ebay"}, {"ebay"})
    with SessionLocal() as db:
        watch = db.get(Watch, wid)
        assert watch.platform_state["ebay"]["initialized"]
        assert watch.platform_state["ebay"]["next_check_at"] == future
        assert watch.revision == revision + 1 and watch.tracking_since == tracking
    ebay = SimpleNamespace(search=AsyncMock())
    klein = SimpleNamespace(
        search=AsyncMock(return_value=([], False, "https://www.kleinanzeigen.de/s-suchanfrage.html"))
    )
    await scan_watch(wid, ebay, {"kleinanzeigen": klein}, due_only=True)
    ebay.search.assert_not_awaited()
    klein.search.assert_awaited_once()
    with SessionLocal() as db:
        watch = db.get(Watch, wid)
        assert watch.initialized
        assert watch.platform_state["ebay"]["last_checked_at"] == 123


@pytest.mark.parametrize("changed", ["ebay", "buyee", "kleinanzeigen"])
def test_editing_one_sources_filters_keeps_other_sources_cards_and_pending_alerts(client, auth, changed):
    wid, body, future, revision, _ = seeded_watch(client, auth)
    if changed == "ebay":
        body["filters"]["max_price"] = 50
    else:
        body["market_filters"][changed]["max_price"] = 50
    response = client.put(f"/api/watches/{wid}", headers=auth, json=body)
    assert response.status_code == 200
    remaining = {"ebay", "buyee", "kleinanzeigen"} - {changed}
    assert platforms_with_cards_and_alerts(wid) == (remaining, remaining)
    with SessionLocal() as db:
        watch = db.get(Watch, wid)
        assert not watch.initialized and watch.revision == revision + 1
        assert set(watch.platform_state) == remaining
        assert all(
            state["next_check_at"] == future and state["last_success_at"] == 123
            for state in watch.platform_state.values()
        )


def test_removing_market_keeps_ebay_and_other_market_initialized(client, auth):
    wid, body, future, revision, _ = seeded_watch(client, auth)
    body["platforms"] = ["ebay", "kleinanzeigen"]
    response = client.put(f"/api/watches/{wid}", headers=auth, json=body)
    assert response.status_code == 200 and response.json()["initialized"]
    assert platforms_with_cards_and_alerts(wid) == ({"ebay", "kleinanzeigen"}, {"ebay", "kleinanzeigen"})
    with SessionLocal() as db:
        assert db.get(Watch, wid).next_check_at == future


def test_query_change_still_resets_all_sources(client, auth):
    wid, body, _, revision, _ = seeded_watch(client, auth)
    body["query"] = "different query"
    response = client.put(f"/api/watches/{wid}", headers=auth, json=body)
    assert response.status_code == 200 and not response.json()["initialized"]
    assert platforms_with_cards_and_alerts(wid) == (set(), set())
    with SessionLocal() as db:
        watch = db.get(Watch, wid)
        assert watch.platform_state == {} and watch.revision == revision + 1


async def test_new_market_block_does_not_turn_existing_ebay_updates_into_silent_baseline(client, auth):
    from app.parser.ebay import Listing
    from app.parser.markets import MarketAccessError

    wid, body, _, _, _ = seeded_watch(client, auth, legacy=True)
    body["platforms"] = ["ebay", "buyee"]
    assert client.put(f"/api/watches/{wid}", headers=auth, json=body).status_code == 200
    with SessionLocal() as db:
        watch = db.get(Watch, wid)
        watch.platform_state = {"ebay": watch.platform_state["ebay"] | {"next_check_at": 0}}
        db.commit()
    fresh = Listing(
        "123456789999", "https://www.ebay.com/itm/123456789999", "New Sony", price=10, currency="USD"
    )
    ebay = SimpleNamespace(search=AsyncMock(return_value=([fresh], False, "https://www.ebay.com/sch/i.html")))
    buyee = SimpleNamespace(search=AsyncMock(side_effect=MarketAccessError("HTTP 403", time.time() + 900)))
    await scan_watch(wid, ebay, {"buyee": buyee}, due_only=True)
    with SessionLocal() as db:
        watch = db.get(Watch, wid)
        assert not watch.initialized and watch.platform_state["ebay"]["initialized"]
        item = db.scalar(select(WatchItem).where(WatchItem.ebay_id == fresh.ebay_id))
        assert not item.baseline and item.is_new
        assert db.scalar(select(Notification).where(Notification.item_id == item.id)) is not None
        assert len(list(db.scalars(select(Notification)))) == 2


def test_renaming_search_preserves_schedule_and_alerts(client, auth):
    wid, body, future, revision, _ = seeded_watch(client, auth)
    body["name"] = "Renamed search"
    response = client.put(f"/api/watches/{wid}", headers=auth, json=body)
    assert response.status_code == 200 and response.json()["initialized"]
    assert response.json()["next_check_at"] == future
    assert platforms_with_cards_and_alerts(wid) == (
        {"ebay", "buyee", "kleinanzeigen"},
        {"ebay", "buyee", "kleinanzeigen"},
    )
    with SessionLocal() as db:
        watch = db.get(Watch, wid)
        assert watch.revision == revision and watch.last_checked_at == 123


def test_legacy_empty_market_budgets_are_equivalent_to_default_budget_payload(client, auth):
    wid, body, future, revision, _ = seeded_watch(client, auth)
    with SessionLocal() as db:
        db.get(Watch, wid).market_filters = {}
        db.commit()
    body["market_filters"] = {}
    response = client.put(f"/api/watches/{wid}", headers=auth, json=body)
    assert response.status_code == 200 and response.json()["initialized"]
    assert response.json()["next_check_at"] == future
    assert platforms_with_cards_and_alerts(wid) == (
        {"ebay", "buyee", "kleinanzeigen"},
        {"ebay", "buyee", "kleinanzeigen"},
    )
    with SessionLocal() as db:
        assert db.get(Watch, wid).revision == revision


def test_interval_change_reschedules_without_resetting_baselines_or_alerts(client, auth):
    wid, body, future, revision, _ = seeded_watch(client, auth)
    body["interval_seconds"] = 600
    response = client.put(f"/api/watches/{wid}", headers=auth, json=body)
    assert response.status_code == 200 and response.json()["initialized"]
    assert response.json()["next_check_at"] < future
    assert platforms_with_cards_and_alerts(wid) == (
        {"ebay", "buyee", "kleinanzeigen"},
        {"ebay", "buyee", "kleinanzeigen"},
    )
    with SessionLocal() as db:
        assert db.get(Watch, wid).revision == revision
