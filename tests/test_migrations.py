import pytest
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import IntegrityError

from app.db import Base
from app.listing_identity import listing_identity
from app.migrations import initialize_schema
from app.models import Notification, SeenListing, User, Watch, WatchItem


@pytest.fixture
def legacy_database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/legacy.db")
    data = {"ebay_id": "123456789001", "url": "https://www.ebay.com/itm/123456789001",
            "title": "Sony", "price": 90, "currency": "USD", "location": "Japan"}
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        for model in (User, Watch, WatchItem):
            model.__table__.create(connection)
        connection.exec_driver_sql("CREATE TABLE notifications (id INTEGER PRIMARY KEY, "
            "item_id INTEGER NOT NULL UNIQUE REFERENCES watch_items(id) ON DELETE CASCADE, "
            "attempts INTEGER NOT NULL, next_attempt_at FLOAT NOT NULL, sent_at FLOAT, error TEXT)")
        connection.exec_driver_sql("CREATE INDEX ix_notifications_next_attempt_at ON notifications(next_attempt_at)")
        connection.execute(insert(User).values(id=1, telegram_id=1, chat_id=1, username="test_user"))
        connection.execute(insert(Watch).values(id=1, user_id=1, name="Sony", query="sony", filters={}, initialized=True))
        connection.execute(insert(WatchItem).values(id=1, watch_id=1, ebay_id=data["ebay_id"], data=data,
            baseline=True, matches=True, first_seen_at=100))
        connection.exec_driver_sql("INSERT INTO notifications VALUES (1, 1, 2, 123, NULL, 'Retry pending')")
    yield engine, data
    engine.dispose()


def upgrade(engine):
    with engine.begin() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        initialize_schema(connection, Base.metadata)


def test_upgrade_preserves_cards_and_outbox_and_allows_new_prices(legacy_database):
    engine, data = legacy_database
    upgrade(engine)
    upgrade(engine)  # Restarting any service must not replay or alter the migration.
    with engine.begin() as connection:
        assert connection.scalar(select(func.count()).select_from(WatchItem)) == 1
        assert connection.scalar(select(func.count()).select_from(SeenListing)) == 1
        alert = connection.execute(select(Notification)).mappings().one()
        assert alert["data"] == data
        assert alert["attempts"] == 2 and alert["next_attempt_at"] == 123
        assert alert["sent_at"] is None and alert["error"] == "Retry pending"
        assert alert["seen_listing_id"] is not None
        assert connection.scalar(select(SeenListing.first_seen_at)) == 100
        fresh = data | {"price": 80}
        seen = connection.scalar(insert(SeenListing).values(user_id=1, **listing_identity(fresh)).returning(SeenListing.id))
        connection.execute(insert(Notification).values(item_id=1, seen_listing_id=seen, data=fresh))
        assert connection.scalar(select(func.count()).select_from(Notification)) == 2
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(insert(Notification).values(item_id=1, seen_listing_id=seen, data=fresh))
    with engine.begin() as connection:
        connection.execute(Watch.__table__.delete().where(Watch.id == 1))
        assert connection.scalar(select(func.count()).select_from(SeenListing)) == 2
        assert connection.scalar(select(func.count()).select_from(Notification)) == 0


def test_failed_upgrade_rolls_back_schema_and_data(legacy_database, monkeypatch):
    engine, _ = legacy_database
    def fail(*args, **kwargs):
        raise RuntimeError("Simulated migration interruption")
    monkeypatch.setattr(Notification.__table__, "create", fail)
    with pytest.raises(RuntimeError, match="interruption"):
        upgrade(engine)
    with engine.connect() as connection:
        assert "notifications_legacy" not in inspect(connection).get_table_names()
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM notifications").scalar() == 1
        assert any(c["column_names"] == ["item_id"] for c in inspect(connection).get_unique_constraints("notifications"))


def test_browser_status_column_preserves_existing_shared_pause(legacy_database):
    engine, _ = legacy_database
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE monitor_state (id INTEGER PRIMARY KEY, retry_at FLOAT NOT NULL, "
                                   "checking_watch_id INTEGER, error TEXT)")
        connection.exec_driver_sql("INSERT INTO monitor_state VALUES (1, 1234567890, 1, 'HTTP 403')")
    upgrade(engine)
    upgrade(engine)
    with engine.connect() as connection:
        row = connection.exec_driver_sql("SELECT retry_at, checking_watch_id, error, browser FROM monitor_state").one()
        assert tuple(row) == (1234567890, 1, "HTTP 403", None)
