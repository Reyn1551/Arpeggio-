import re
import time
from datetime import UTC, datetime, timedelta, timezone

import pytest

from arpeggio_ai.core.clock import format_utc, utc_now
from arpeggio_ai.core.ids import new_id

ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid_ms(value: str) -> int:
    number = 0
    for char in value:
        number = number * 32 + ALPHABET.index(char)
    return number >> 80


def test_new_id_is_a_ulid() -> None:
    value = new_id()
    assert ULID.fullmatch(value)
    assert value[0] <= "7"  # 128 bits in 130: the top character never exceeds 7


def test_new_id_encodes_the_current_time() -> None:
    before = time.time_ns() // 1_000_000
    value = new_id()
    after = time.time_ns() // 1_000_000
    assert before <= ulid_ms(value) <= after


def test_ids_sort_by_time_across_milliseconds() -> None:
    ids = [new_id(now_ms=ms) for ms in (1, 2, 1_700_000_000_000, (1 << 48) - 1)]
    assert ids == sorted(ids)
    assert [ulid_ms(i) for i in ids] == [1, 2, 1_700_000_000_000, (1 << 48) - 1]


def test_ids_are_unique() -> None:
    assert len({new_id(now_ms=5) for _ in range(1000)}) == 1000


@pytest.mark.parametrize("ms", [-1, 1 << 48])
def test_timestamp_must_fit_48_bits(ms: int) -> None:
    with pytest.raises(ValueError):
        new_id(now_ms=ms)


def test_format_utc_is_fixed_width_with_milliseconds() -> None:
    moment = datetime(2026, 1, 2, 3, 4, 5, 6789, tzinfo=UTC)
    assert format_utc(moment) == "2026-01-02T03:04:05.006Z"


def test_format_utc_converts_other_zones() -> None:
    jakarta = timezone(timedelta(hours=7))
    moment = datetime(2026, 10, 7, 7, 0, 0, tzinfo=jakarta)
    assert format_utc(moment) == "2026-10-07T00:00:00.000Z"


def test_format_utc_rejects_naive_datetimes() -> None:
    with pytest.raises(ValueError):
        format_utc(datetime(2026, 1, 1))


def test_utc_now_format_and_value() -> None:
    value = utc_now()
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", value)
    parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    assert abs(datetime.now(UTC) - parsed) < timedelta(seconds=5)
