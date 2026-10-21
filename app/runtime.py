import fcntl
import logging

from app.config import get_settings
from app.db import session_scope, stamp
from app.models import ServiceState


def process_lock(name):
    handle = (get_settings().state_dir / f"{name}.lock").open("w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(f"Another {name} process already uses this data directory") from None
    return handle


def heartbeat(name: str, error: str | None = None, value: int | None = None):
    with session_scope() as db:
        state = db.get(ServiceState, name)
        if state is None:
            state = ServiceState(name=name)
            db.add(state)
        state.heartbeat_at = stamp()
        state.error = error
        if value is not None:
            state.value = value


def configure_logging():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

