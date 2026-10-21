import os
import tempfile

_test_dir = tempfile.mkdtemp(prefix="ebay-signal-test-")
os.environ.update(DATABASE_URL=f"sqlite:///{_test_dir}/test.db", STATE_DIR=_test_dir,
                  SECRET_KEY="unit-tests-only-" + "a" * 48, TELEGRAM_BOT_TOKEN="test-token",
                  TELEGRAM_BOT_USERNAME="signal_test_bot")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import User  # noqa: E402
from app.security import new_tokens  # noqa: E402


@pytest.fixture(autouse=True)
def database():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def client():
    with TestClient(app) as client:
        yield client


@pytest.fixture
def user():
    with SessionLocal() as db:
        user = User(telegram_id=123456, chat_id=123456, username="test_user")
        db.add(user)
        db.commit()
        return user


@pytest.fixture
def auth(user):
    with SessionLocal() as db:
        access, _ = new_tokens(db, user)
        db.commit()
        return {"Authorization": "Bearer " + access}
