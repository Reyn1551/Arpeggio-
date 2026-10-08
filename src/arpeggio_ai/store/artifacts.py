"""Large payloads (full tool outputs, diffs, logs) kept as files under ``artifacts/``.

Layout: ``<root>/<task_id>/<attempt_id>/<name>``. The database stores only the relative
reference returned by ``write``. Files are never overwritten.

Every text artifact passes the secret scanner before it is written (NFR-06): findings are
replaced with ``[REDACTED:<type>]`` and a ``secret_scan.redacted`` event names the
artifact. Data counts as text unless its first 8 KiB hold a NUL byte. Text that is not
valid UTF-8 (a check's output in a legacy code page, say) is scanned too, and its other
bytes are written back unchanged.
"""

import re
from pathlib import Path

from arpeggio_ai.core.errors import StoreError
from arpeggio_ai.safety.secret_scan import current_scanner

DIR_MODE = 0o700
TEXT_PROBE_BYTES = 8192
_SAFE_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _check_part(part: str) -> None:
    # Rejects separators, "..", empty and hidden names, so a ref can never leave the root.
    if _SAFE_PART.fullmatch(part) is None:
        raise StoreError(f"invalid artifact path component: {part!r}")


def redact_artifact(name: str, data: bytes) -> bytes:
    """``data`` with secrets redacted, or unchanged if it is binary."""
    if b"\0" in data[:TEXT_PROBE_BYTES]:
        return data
    text = data.decode("utf-8", errors="surrogateescape")
    redacted = current_scanner().redact(text, f"artifact:{name}")
    return data if redacted == text else redacted.encode("utf-8", errors="surrogateescape")


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root.absolute()

    def write(self, task_id: str, attempt_id: str, name: str, data: bytes) -> str:
        """Redact, write a new artifact and return its reference, ``task_id/attempt_id/name``."""
        parts = (task_id, attempt_id, name)
        for part in parts:
            _check_part(part)
        ref = "/".join(parts)
        target = self.root.joinpath(*parts)
        for directory in (self.root, target.parent.parent, target.parent):
            if not directory.is_dir():
                directory.mkdir(mode=DIR_MODE, parents=True)
                directory.chmod(DIR_MODE)  # mkdir's mode is reduced by the umask
        try:
            with target.open("xb") as handle:
                handle.write(redact_artifact(name, data))
        except FileExistsError:
            raise StoreError(f"artifact already exists: {ref}") from None
        return ref

    def path(self, ref: str) -> Path:
        parts = ref.split("/")
        if len(parts) != 3:
            raise StoreError(f"artifact ref must be task/attempt/name: {ref!r}")
        for part in parts:
            _check_part(part)
        return self.root.joinpath(*parts)

    def read(self, ref: str) -> bytes:
        try:
            return self.path(ref).read_bytes()
        except FileNotFoundError:
            raise StoreError(f"artifact not found: {ref}") from None
