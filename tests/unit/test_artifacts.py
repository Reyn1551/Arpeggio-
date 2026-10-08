import os
import stat
import sys
from pathlib import Path

import pytest
from fakes import FAKE_KEY

from arpeggio_ai.core.errors import StoreError
from arpeggio_ai.paths import artifacts_dir
from arpeggio_ai.safety.secret_scan import SecretScanner, use_scanner
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


# Content secret scan (NFR-06)

AWS = "AKIA" + "Q7RZ3MX9KD2LPW5T"


def test_text_artifacts_are_redacted(
    store: ArtifactStore, caplog: pytest.LogCaptureFixture
) -> None:
    with use_scanner(SecretScanner(configured=[FAKE_KEY])):
        ref = store.write("T1", "A1", "check-1.log", f"{AWS}\nkey={FAKE_KEY}\n".encode())
    assert store.read(ref) == b"[REDACTED:aws_access_key]\nkey=[REDACTED:configured_key]\n"
    [record] = [r for r in caplog.records if r.getMessage() == "secret_scan.redacted"]
    assert record.__dict__["source"] == "artifact:check-1.log"
    assert record.__dict__["types"] == {"aws_access_key": 1, "configured_key": 1}


def test_non_utf8_text_is_scanned_and_other_bytes_kept(store: ArtifactStore) -> None:
    data = "café ".encode("cp1252") + AWS.encode() + b" \xff\r\n"
    ref = store.write("T1", "A1", "check-2.log", data)
    assert store.read(ref) == b"caf\xe9 [REDACTED:aws_access_key] \xff\r\n"


def test_binary_artifacts_are_not_scanned(store: ArtifactStore) -> None:
    data = b"\x00\x01" + AWS.encode()
    ref = store.write("T1", "A1", "blob.bin", data)
    assert store.read(ref) == data


def test_clean_text_is_written_byte_for_byte(store: ArtifactStore) -> None:
    data = b"line one\r\nline two\n\xff"
    assert store.read(store.write("T1", "A1", "plain.txt", data)) == data
