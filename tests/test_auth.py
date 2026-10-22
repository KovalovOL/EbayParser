import time

from sqlalchemy import select

from app.bot import handle_update
from app.db import SessionLocal
from app.models import LoginChallenge, User
from app.security import digest


class FakeTelegram:
    def __init__(self): self.messages = []
    async def send(self, chat_id, text): self.messages.append((chat_id,text))


def challenge(client):
    r = client.post("/api/auth/start",json={"username":"@test_user"})
    assert r.status_code == 200
    result = r.json()
    assert "code" not in result
    assert "test-token" not in str(result)
    return result["challenge_id"]


def delivered(challenge_id):
    with SessionLocal() as db:
        row = db.get(LoginChallenge,challenge_id)
        row.code_hash = digest(challenge_id+":123456")
        row.telegram_id = 112233
        row.chat_id = 112233
        db.commit()


def test_login_refresh_rotation_and_reuse(client):
    cid = challenge(client)
    delivered(cid)
    r = client.post("/api/auth/verify",json={"challenge_id":cid,"code":"123456"})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    token = r.json()["access_token"]
    assert client.get("/api/me",headers={"Authorization":"Bearer "+token}).json()["username"] == "test_user"
    old = client.cookies.get("refresh_token")
    assert client.post("/api/auth/refresh").status_code == 200
    new = client.cookies.get("refresh_token")
    assert old != new
    client.cookies.set("refresh_token",old,domain="testserver.local",path="/api/auth")
    assert client.post("/api/auth/refresh").status_code == 401
    client.cookies.set("refresh_token",new,domain="testserver.local",path="/api/auth")
    assert client.post("/api/auth/refresh").status_code == 401
    assert client.post("/api/auth/verify",json={"challenge_id":cid,"code":"123456"}).status_code == 400


def test_guess_limit_and_expiry(client):
    cid = challenge(client)
    delivered(cid)
    for _ in range(5):
        assert client.post("/api/auth/verify",json={"challenge_id":cid,"code":"000000"}).status_code == 400
    assert client.post("/api/auth/verify",json={"challenge_id":cid,"code":"123456"}).status_code == 400


def test_login_throttled(client):
    challenge(client)
    assert client.post("/api/auth/start",json={"username":"test_user"}).status_code == 429


def test_origin_and_missing_auth(client):
    assert client.post("/api/auth/refresh",headers={"Origin":"https://evil.test"}).status_code == 403
    assert client.get("/api/watches").status_code == 401


async def test_bot_rejects_username_mismatch(client):
    cid = challenge(client)
    tg = FakeTelegram()
    await handle_update({"message":{"chat":{"type":"private","id":999},"from":{"id":999,"username":"attacker"},"text":"/start login_"+cid}},tg)
    with SessionLocal() as db:
        assert db.get(LoginChallenge,cid).code_hash is None
    assert "does not match" in tg.messages[0][1]


async def test_bot_generates_code_only_for_matching_private_user(client):
    cid = challenge(client)
    tg = FakeTelegram()
    update = {"message":{"chat":{"type":"private","id":123},"from":{"id":123,"username":"TEST_USER"},"text":"/start login_"+cid}}
    await handle_update(update,tg)
    import re
    code = re.search(r"<code>(\d{6})</code>",tg.messages[0][1])[1]
    assert client.post("/api/auth/verify",json={"challenge_id":cid,"code":code}).status_code == 200
    with SessionLocal() as db:
        assert db.scalar(select(User)).telegram_id == 123


def test_logout_revokes_refresh(client):
    cid = challenge(client); delivered(cid)
    client.post("/api/auth/verify",json={"challenge_id":cid,"code":"123456"})
    old = client.cookies.get("refresh_token")
    assert client.post("/api/auth/logout").status_code == 200
    client.cookies.set("refresh_token",old,domain="testserver.local",path="/api/auth")
    assert client.post("/api/auth/refresh").status_code == 401


def test_expired_code(client):
    cid = challenge(client); delivered(cid)
    with SessionLocal() as db:
        db.get(LoginChallenge,cid).expires_at = time.time() - 1
        db.commit()
    assert client.post("/api/auth/verify",json={"challenge_id":cid,"code":"123456"}).status_code == 400


def test_login_delivery_status_does_not_expose_code_or_identity(client):
    cid = challenge(client)
    url = f"/api/auth/challenges/{cid}"
    response = client.get(url)
    assert response.headers["cache-control"] == "no-store"
    status = response.json()
    assert set(status) == {"status", "expires_in", "attempts_remaining"}
    assert status["status"] == "waiting" and 0 < status["expires_in"] <= 600
    delivered(cid)
    assert client.get(url).json()["status"] == "sent"
    for _ in range(5):
        client.post("/api/auth/verify",json={"challenge_id":cid,"code":"000000"})
    assert client.get(url).json()["status"] == "locked"
    assert client.get(url).json()["attempts_remaining"] == 0
    assert client.get("/api/auth/challenges/not-a-real-challenge").status_code == 404


def test_login_status_tracks_used_and_expired_requests(client):
    cid = challenge(client); delivered(cid)
    client.post("/api/auth/verify",json={"challenge_id":cid,"code":"123456"})
    assert client.get(f"/api/auth/challenges/{cid}").json()["status"] == "used"
    with SessionLocal() as db:
        row = db.get(LoginChallenge,cid)
        row.used = False; row.expires_at = time.time() - 1
        db.commit()
    status = client.get(f"/api/auth/challenges/{cid}").json()
    assert status["status"] == "expired" and status["expires_in"] == 0


async def test_bot_help_explains_registration_and_notification_controls(client):
    tg = FakeTelegram()
    await handle_update({"message":{"chat":{"type":"private","id":123},
                                 "from":{"id":123,"username":"test_user"},"text":"/help"}},tg)
    message = tg.messages[0][1]
    assert "Mira Tarel0chn1tsa" in message
    assert "press Start" in message and "pause the search" in message
    assert "created automatically" in message
    assert "localhost" not in message
