"""UTC timestamp helpers shared by every ingest path and the conflict resolver."""
from datetime import datetime, timezone


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_utc_timestamp(ts: object) -> datetime | None:
    """Parse an ISO-8601 UTC timestamp into an aware datetime.

    Returns ``None`` for missing/empty/unparseable values so callers can treat
    "no comparable timestamp" uniformly. A naive datetime (no tzinfo) is
    assumed to be UTC.
    """
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
