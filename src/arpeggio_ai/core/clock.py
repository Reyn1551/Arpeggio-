"""UTC timestamps as fixed-width ISO-8601 text, so string order equals time order."""

from datetime import UTC, datetime


def format_utc(moment: datetime) -> str:
    """Format an aware datetime as ``YYYY-MM-DDTHH:MM:SS.mmmZ``."""
    if moment.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    utc = moment.astimezone(UTC)
    return f"{utc:%Y-%m-%dT%H:%M:%S}.{utc.microsecond // 1000:03d}Z"


def utc_now() -> str:
    return format_utc(datetime.now(UTC))
