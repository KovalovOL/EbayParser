"""Transactional SQLite upgrade from item-ID alerts to account-scoped composite identities."""
import json

from sqlalchemy import inspect, select
from sqlalchemy.dialects.sqlite import insert

from app.listing_identity import listing_identity


def initialize_schema(connection, metadata):
    from app.models import Notification, SeenListing, Watch, WatchItem

    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    new_seen_table = "seen_listings" not in tables
    upgrade_alerts = "notifications" in tables and any(
        constraint["column_names"] == ["item_id"]
        for constraint in inspector.get_unique_constraints("notifications")
    )
    metadata.create_all(connection)
    if "monitor_state" in tables and "browser" not in {
        column["name"] for column in inspector.get_columns("monitor_state")
    }:
        connection.exec_driver_sql("ALTER TABLE monitor_state ADD COLUMN browser VARCHAR(16)")
    if not (new_seen_table or upgrade_alerts):
        return

    # Seed already saved cards, preserving their history without creating any alerts.
    for user_id, data, first_seen in connection.execute(
        select(Watch.user_id, WatchItem.data, WatchItem.first_seen_at)
        .join(WatchItem, WatchItem.watch_id == Watch.id)
        .order_by(WatchItem.first_seen_at, WatchItem.id)
    ):
        connection.execute(insert(SeenListing).values(
            user_id=user_id, first_seen_at=first_seen, **listing_identity(data)
        ).on_conflict_do_nothing(index_elements=["user_id", "fingerprint"]))

    if not upgrade_alerts:
        return
    old_alerts = list(connection.exec_driver_sql(
        "SELECT n.id, n.item_id, n.attempts, n.next_attempt_at, n.sent_at, n.error, "
        "i.data AS listing_data, w.user_id FROM notifications n "
        "JOIN watch_items i ON i.id=n.item_id JOIN watches w ON w.id=i.watch_id "
        "ORDER BY n.sent_at IS NULL, n.id"
    ).mappings())
    connection.exec_driver_sql("ALTER TABLE notifications RENAME TO notifications_legacy")
    connection.exec_driver_sql("DROP INDEX IF EXISTS ix_notifications_next_attempt_at")
    Notification.__table__.create(connection)
    claimed = set()
    for old in old_alerts:
        data = json.loads(old["listing_data"]) if isinstance(old["listing_data"], str) else old["listing_data"]
        seen_id = connection.scalar(select(SeenListing.id).where(
            SeenListing.user_id == old["user_id"],
            SeenListing.fingerprint == listing_identity(data)["fingerprint"],
        ))
        values = {key: old[key] for key in ("id", "item_id", "attempts", "next_attempt_at", "sent_at", "error")}
        # Preserve historical duplicate rows. Only one owns the new global identity.
        values.update(data=data, seen_listing_id=seen_id if seen_id not in claimed else None)
        connection.execute(insert(Notification).values(**values))
        claimed.add(seen_id)
    connection.exec_driver_sql("DROP TABLE notifications_legacy")
