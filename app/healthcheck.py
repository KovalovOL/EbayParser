"""Process liveness only; eBay restrictions are reported separately by /api/status."""
import sys

from app.db import session_scope, stamp
from app.models import ServiceState


def healthy(name):
    with session_scope() as db:
        state = db.get(ServiceState, name)
        return bool(state and stamp() - state.heartbeat_at < 120)


if __name__ == "__main__":
    raise SystemExit(0 if healthy(sys.argv[1]) else 1)
