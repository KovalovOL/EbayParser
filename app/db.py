import time
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def stamp() -> float:
    return time.time()


class Base(DeclarativeBase):
    pass


settings = get_settings()
settings.state_dir.mkdir(parents=True, exist_ok=True)
engine = create_engine(settings.database_url, connect_args={"check_same_thread": False, "timeout": 30})


@event.listens_for(engine, "connect")
def configure_sqlite(connection, _):
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA journal_mode=WAL")


SessionLocal = sessionmaker(engine, expire_on_commit=False)


def init_db():
    from app import models  # noqa: F401
    from app.migrations import initialize_schema
    with engine.begin() as connection:
        # web, bot and worker may start together; serialize schema changes and backfill.
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        initialize_schema(connection, Base.metadata)


def get_db():
    with SessionLocal() as session:
        yield session


@contextmanager
def session_scope():
    with SessionLocal() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise

