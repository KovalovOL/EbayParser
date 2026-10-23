import pytest
from bs4 import BeautifulSoup
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Notification, User, Watch, WatchItem
from app.security import new_tokens


def create(client,auth):
    r=client.post("/api/watches",headers=auth,json={"name":"Walkman","query":"sony walkman","filters":{"max_price":100,"category_id":"15054","attributes":{"Brand":"Sony"}}})
    assert r.status_code == 201
    return r.json()["id"]


def test_crud_and_owner_isolation(client,auth):
    wid=create(client,auth)
    assert len(client.get("/api/watches",headers=auth).json()) == 1
    with SessionLocal() as db:
        other=User(telegram_id=9988,chat_id=9988,username="another_user")
        db.add(other); db.flush()
        token,_=new_tokens(db,other); db.commit()
    headers={"Authorization":"Bearer "+token}
    assert client.get("/api/watches",headers=headers).json() == []
    assert client.get(f"/api/items?watch_id={wid}",headers=headers).status_code == 404
    assert client.patch(f"/api/watches/{wid}",headers=headers,json={"active":False}).status_code == 404
    assert client.delete(f"/api/watches/{wid}",headers=headers).status_code == 404
    assert client.delete(f"/api/watches/{wid}",headers=auth).status_code == 204


def test_filter_change_resets_baseline_and_cascades_notifications(client,auth):
    wid=create(client,auth)
    with SessionLocal() as db:
        db.get(Watch,wid).initialized=True
        item=WatchItem(watch_id=wid,ebay_id="123456789001",data={},baseline=False,matches=True)
        db.add(item); db.flush(); db.add(Notification(item_id=item.id)); db.commit()
    r=client.put(f"/api/watches/{wid}",headers=auth,json={"name":"Changed","query":"aiwa","filters":{}})
    assert r.status_code == 200 and not r.json()["initialized"]
    with SessionLocal() as db:
        assert db.scalar(select(WatchItem)) is None
        assert db.scalar(select(Notification)) is None


def test_new_filters_round_trip_and_region_change_resets_snapshot(client, auth):
    payload = {"name":"Camera", "query":"sony camera", "filters":{
        "location_region":"europe", "accepts_offers":True, "returns_accepted":True,
        "free_returns":True, "sale_items":True}}
    response = client.post("/api/watches", headers=auth, json=payload)
    assert response.status_code == 201
    saved = response.json()
    assert all(saved["filters"][key] == value for key, value in payload["filters"].items())
    assert client.get("/api/watches", headers=auth).json()[0]["filters"] == saved["filters"]
    with SessionLocal() as db:
        db.get(Watch, saved["id"]).initialized = True
        db.commit()
    payload["filters"]["location_region"] = "asia"
    updated = client.put(f'/api/watches/{saved["id"]}', headers=auth, json=payload)
    assert updated.status_code == 200 and not updated.json()["initialized"]
    assert updated.json()["filters"]["location_region"] == "asia"


@pytest.mark.parametrize("filters", [
    {"location_region":"invalid"}, {"location_region":"custom"},
    {"location_region":"custom", "location":"  "},
    {"location_region":"europe", "location":"Japan"},
])
def test_invalid_or_conflicting_item_locations_are_rejected(client, auth, filters):
    response = client.post("/api/watches", headers=auth, json={"name":"x", "query":"x", "filters":filters})
    assert response.status_code == 422


def test_editing_legacy_filters_without_changes_keeps_saved_results(client, auth):
    wid = create(client, auth)
    new_fields = {"location_region", "accepts_offers", "returns_accepted", "free_returns", "sale_items"}
    with SessionLocal() as db:
        watch = db.get(Watch, wid)
        watch.filters = {key: value for key, value in watch.filters.items() if key not in new_fields} | {
            "location":"Japan"}
        watch.initialized = True
        db.add(WatchItem(watch_id=wid, ebay_id="123456789001", data={}, baseline=True, matches=True))
        db.commit()
    saved = client.get("/api/watches", headers=auth).json()[0]
    assert saved["filters"]["location_region"] == "custom"
    assert not saved["filters"]["accepts_offers"]
    payload = {key: saved[key] for key in ("name", "query", "filters", "interval_seconds")}
    payload["name"] = "Renamed legacy search"
    updated = client.put(f"/api/watches/{wid}", headers=auth, json=payload)
    assert updated.status_code == 200 and updated.json()["initialized"]
    with SessionLocal() as db:
        assert db.scalar(select(WatchItem).where(WatchItem.watch_id == wid)) is not None


def test_pause_and_resume_resets_tracking_window(client,auth):
    wid=create(client,auth)
    assert client.patch(f"/api/watches/{wid}",headers=auth,json={"active":False}).status_code == 200
    assert client.post(f"/api/watches/{wid}/check",headers=auth).status_code == 400
    r=client.patch(f"/api/watches/{wid}",headers=auth,json={"active":True})
    assert r.status_code == 200 and not r.json()["initialized"]


def test_validation_and_pagination(client,auth):
    assert client.post("/api/watches",headers=auth,json={"name":"x","query":"x","filters":{"min_price":100,"max_price":10}}).status_code == 422
    assert client.post("/api/watches",headers=auth,json={"name":"x","query":"x","interval_seconds":1}).status_code == 422
    assert client.get("/api/items?limit=10000",headers=auth).status_code == 422
    assert client.get("/api/items?offset=-1",headers=auth).status_code == 422
    assert client.post("/api/watches",headers=auth,json={"name":"x","query":"x","filters":{"attributes":{"Brand":"Sony"}}}).status_code == 422


def test_item_page_fetching_endpoint_is_not_available(client,auth):
    assert client.post("/api/items/1/details",headers=auth).status_code == 404
    status = client.get("/api/status",headers=auth).json()
    assert status["search_pages_per_check"] == 1 and not status["automatic_item_details"]
    assert status["newness_basis"] == "title_price_location_per_user"


def test_page_and_static_assets(client):
    r=client.get("/")
    assert r.status_code == 200
    assert "Mira Tarel0chn1tsa" in r.text and '<html lang="en">' in r.text
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-cache"
    soup = BeautifulSoup(r.text, "html.parser")
    for url in (soup.select_one('link[rel="stylesheet"]')["href"], soup.select_one("script[src]")["src"]):
        assert "?v=" in url
        asset = client.get(url)
        assert asset.status_code == 200 and asset.headers["cache-control"] == "no-cache"
    assert client.get("/api/health").json() == {"status":"ok"}


def test_updated_assets_get_new_urls_even_without_a_server_restart(client, tmp_path, monkeypatch):
    from app import main

    static = tmp_path / "static"
    static.mkdir()
    (static / "style.css").write_text("css-v1")
    (static / "app.js").write_text("js-v1")
    monkeypatch.setattr(main, "ROOT", tmp_path)

    def urls():
        soup = BeautifulSoup(client.get("/").text, "html.parser")
        return soup.select_one('link[rel="stylesheet"]')["href"], soup.select_one("script[src]")["src"]

    initial_css, initial_js = urls()
    assert urls() == (initial_css, initial_js)
    (static / "app.js").write_text("js-v2")
    css, js = urls()
    assert css == initial_css and js != initial_js
    (static / "style.css").write_text("css-v2")
    updated_css, updated_js = urls()
    assert updated_css != css and updated_js == js


def test_old_system_errors_display_in_english_without_changing_user_content(client,auth):
    wid = create(client,auth)
    with SessionLocal() as db:
        watch = db.get(Watch,wid)
        watch.name = "Мій пошук"
        watch.error = "eBay обмежив доступ (HTTP 403). Усі запити до eBay призупинено щонайменше на 15 хв."
        db.add(WatchItem(watch_id=wid,ebay_id="123456789001",matches=True,is_new=True,baseline=False,
                         data={"title":"Товар", "attributes":{}},detail_error="Не розпізнано сторінку товару eBay; характеристики поки недоступні."))
        db.commit()
    watch = client.get("/api/watches",headers=auth).json()[0]
    assert watch["name"] == "Мій пошук"
    assert watch["error"] == "eBay restricted access (HTTP 403). All eBay requests are paused for at least 15 min."
    item = client.get("/api/items",headers=auth).json()["items"][0]
    assert item["title"] == "Товар" and item["detail_error"].startswith("The eBay item format")
    assert not item["notification_pending"] and not item["notification_sent"]
    with SessionLocal() as db:
        db.add(Notification(item_id=item["id"]))
        db.commit()
    assert client.get("/api/items",headers=auth).json()["items"][0]["notification_pending"]
