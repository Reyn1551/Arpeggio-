"""Glob matching and the scan report shape (SAF-02)."""

import pytest

from arpeggio_ai.safety.fileset import ScanReport, glob_match


@pytest.mark.parametrize(
    ("pattern", "path", "matched"),
    [
        ("tests/**", "tests/a.py", True),
        ("tests/**", "tests/unit/a.py", True),
        ("tests/**", "src/tests/a.py", False),
        ("**/test_*.py", "test_a.py", True),
        ("**/test_*.py", "pkg/sub/test_a.py", True),
        ("**/test_*.py", "pkg/test_a.txt", False),
        ("**/*Test.php", "tests/Feature/EmailTest.php", True),
        ("**/*.spec.*", "web/app.spec.ts", True),
        ("**/*.test.*", "web/app.ts", False),
        ("*.py", "a.py", True),
        ("*.py", "pkg/a.py", False),
        ("src/?.py", "src/a.py", True),
        ("a+b/[x].md", "a+b/[x].md", True),
    ],
)
def test_glob_match(pattern: str, path: str, matched: bool) -> None:
    assert glob_match(pattern, path) is matched


def test_report_totals_top_files_and_dict_shape() -> None:
    report = ScanReport(files_scanned=3, skipped={"binary": 1})
    report.findings = [
        ("b.py", 1, "aws_access_key"),
        ("a.py", 2, "assigned_secret"),
        ("b.py", 9, "assigned_secret"),
    ]
    assert report.totals() == {"assigned_secret": 2, "aws_access_key": 1}
    assert report.top_files() == [("b.py", 2), ("a.py", 1)]
    data = report.to_dict()
    assert set(data) == {"files_scanned", "skipped", "totals", "top_files", "findings"}
    assert data["findings"] == [
        {"file": "b.py", "line": 1, "type": "aws_access_key"},
        {"file": "a.py", "line": 2, "type": "assigned_secret"},
        {"file": "b.py", "line": 9, "type": "assigned_secret"},
    ]
    assert report.top_files(limit=1) == [("b.py", 2)]
