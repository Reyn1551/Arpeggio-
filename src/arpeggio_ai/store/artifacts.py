"""Large payloads (full tool outputs, diffs, logs) kept as files under ``artifacts/``.

Layout: ``<root>/<task_id>/<attempt_id>/<name>``. The database stores only the relative
reference returned by ``write``. Files are never overwritten.
"""

import re
from pathlib import Path

from arpeggio_ai.core.errors import StoreError

DIR_MODE = 0o700
_SAFE_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _check_part(part: str) -> None:
    # Rejects separators, "..", empty and hidden names, so a ref can never leave the root.
    if _SAFE_PART.fullmatch(part) is None:
        raise StoreError(f"invalid artifact path component: {part!r}")


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root.absolute()

    def write(self, task_id: str, attempt_id: str, name: str, data: bytes) -> str:
        """Write a new artifact and return its reference, ``task_id/attempt_id/name``."""
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
                handle.write(data)
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
