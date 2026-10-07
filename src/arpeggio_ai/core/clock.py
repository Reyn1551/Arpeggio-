"""UTC timestamps as fixed-width ISO-8601 text, so string order equals time order."""

import re
from datetime import UTC, datetime

_UTC_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")


def format_utc(moment: datetime) -> str:
    """Format an aware datetime as ``YYYY-MM-DDTHH:MM:SS.mmmZ``."""
    if moment.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    utc = moment.astimezone(UTC)
    return f"{utc:%Y-%m-%dT%H:%M:%S}.{utc.microsecond // 1000:03d}Z"


def utc_now() -> str:
    return format_utc(datetime.now(UTC))


def is_utc_timestamp(value: str) -> bool:
    """True if ``value`` is a real moment in the ``YYYY-MM-DDTHH:MM:SS.mmmZ`` form."""
    if _UTC_TIMESTAMP.fullmatch(value) is None:
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        return False
    return True
