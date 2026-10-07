"""Resolve secret references from config (``env:NAME``) into values.

Values come back as ``pydantic.SecretStr``, whose ``repr`` and ``str`` hide the value. Call
``get_secret_value()`` only where the value is sent, such as an HTTP header. Error messages
name the variable, never the value.
"""

import os
import re
from collections.abc import Mapping

from pydantic import SecretStr

from arpeggio_ai.core.errors import SecretNotFound

_REFERENCE = re.compile(r"(?P<scheme>env|keychain):(?P<name>[A-Za-z_][A-Za-z0-9_.-]*)")


def resolve(ref: str, env: Mapping[str, str] | None = None) -> SecretStr:
    """Return the secret that ``ref`` points at.

    ``env:NAME`` reads the environment variable NAME (``env`` replaces ``os.environ`` in
    tests). A variable that is unset or empty raises ``SecretNotFound``.
    """
    match = _REFERENCE.fullmatch(ref)
    if match is None:
        # Never echo ref: a malformed reference may be a pasted key.
        raise ValueError("secret reference must look like env:NAME")
    if match["scheme"] == "keychain":
        raise NotImplementedError("keychain references are not supported yet")
    name = match["name"]
    value = (os.environ if env is None else env).get(name)
    if not value:
        raise SecretNotFound(f"environment variable {name} is not set")
    return SecretStr(value)


def reference_name(ref: str) -> str:
    """The variable or entry name in a reference (``env:NAME`` -> ``NAME``), for messages."""
    match = _REFERENCE.fullmatch(ref)
    return match["name"] if match else "(invalid reference)"
