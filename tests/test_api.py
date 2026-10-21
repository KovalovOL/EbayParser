from sqlalchemy import select

from app.db import SessionLocal
from app.models import Notification, User, Watch, WatchItem
from app.security import new_tokens


def create(client,auth):
    r=client.post("/api/watches",headers=auth,json={"name":"Walkman","query":"sony walkman","filters":{"max_price":100,"attributes":{"Brand":"Sony"}}})
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


def test_page_and_static_assets(client):
    r=client.get("/")
    assert r.status_code == 200
    assert "signal" in r.text
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/api/health").json() == {"status":"ok"}
