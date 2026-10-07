import os
import stat
import sys
from pathlib import Path

import pytest

from arpeggio_ai.core.errors import StoreError
from arpeggio_ai.paths import artifacts_dir
from arpeggio_ai.store.artifacts import ArtifactStore


@pytest.fixture
def store(home: Path) -> ArtifactStore:
    return ArtifactStore(artifacts_dir())


def test_artifacts_dir_is_under_home(home: Path, tmp_path: Path) -> None:
    assert artifacts_dir() == home / "artifacts"
    assert artifacts_dir(tmp_path) == tmp_path / "artifacts"


def test_write_and_read_round_trip(store: ArtifactStore, home: Path) -> None:
    data = b"\x00binary\r\nand text\n"
    ref = store.write("T1", "A1", "step-0001.json", data)
    assert ref == "T1/A1/step-0001.json"
    assert store.read(ref) == data
    assert store.path(ref) == home / "artifacts" / "T1" / "A1" / "step-0001.json"


def test_never_overwrites(store: ArtifactStore) -> None:
    ref = store.write("T1", "A1", "out.log", b"first")
    with pytest.raises(StoreError, match="already exists"):
        store.write("T1", "A1", "out.log", b"second")
    assert store.read(ref) == b"first"


@pytest.mark.parametrize("bad", ["", ".", "..", ".hidden", "a/b", "a\\b", "x y", "-x"])
def test_unsafe_names_are_rejected(store: ArtifactStore, bad: str) -> None:
    with pytest.raises(StoreError, match="invalid artifact path component"):
        store.write("T1", "A1", bad, b"x")
    with pytest.raises(StoreError, match="invalid artifact path component"):
        store.write(bad, "A1", "ok.txt", b"x")


@pytest.mark.parametrize("ref", ["T1/A1", "T1/A1/x/y", "T1/../x", "../A1/x", "/T1/A1/x"])
def test_bad_refs_are_rejected(store: ArtifactStore, ref: str) -> None:
    with pytest.raises(StoreError):
        store.path(ref)


def test_reading_missing_artifact_is_a_store_error(store: ArtifactStore) -> None:
    with pytest.raises(StoreError, match="artifact not found"):
        store.read("T1/A1/missing.txt")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_new_directories_are_0700(store: ArtifactStore, home: Path) -> None:
    previous = os.umask(0o022)
    try:
        store.write("T1", "A1", "x.txt", b"x")
    finally:
        os.umask(previous)
    for path in (home / "artifacts", home / "artifacts" / "T1", home / "artifacts" / "T1" / "A1"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700, path
