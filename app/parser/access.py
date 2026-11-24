"""Source-wide recovery delays, independent of individual search failures."""
import math
import time
from email.utils import parsedate_to_datetime


def access_retry_at(base: int, failures: int, retry_after: str = "") -> float:
    now = time.time()
    delay = 0.0
    try:
        delay = float(retry_after)
    except (ValueError, TypeError):
        try:
            delay = parsedate_to_datetime(retry_after).timestamp() - now
        except (ValueError, TypeError, OverflowError):
            pass
    if not math.isfinite(delay):
        delay = 0.0
    # Keep the normal first 15-minute pause. Repeated denials need a longer
    # quiet window rather than an unlimited series of identical probes.
    recovery = min(max(base, 4 * 3600), base * 2 ** min(max(failures - 1, 0), 8))
    return now + max(recovery, min(86400, delay))
