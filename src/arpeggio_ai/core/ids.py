"""ULID identifiers: 26 Crockford base32 characters, sortable by creation time."""

import secrets
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_id(now_ms: int | None = None) -> str:
    """Return a new ULID: a 48-bit millisecond timestamp followed by 80 random bits."""
    ms = time.time_ns() // 1_000_000 if now_ms is None else now_ms
    if not 0 <= ms < 1 << 48:
        raise ValueError("ULID timestamp must fit in 48 bits")
    value = (ms << 80) | secrets.randbits(80)
    chars = []
    for _ in range(26):
        chars.append(_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))
