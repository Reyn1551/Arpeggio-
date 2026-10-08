"""Secret scanner detectors, redaction, allowlist and chunking (SAF-02, NFR-06).

Token-shaped test values are assembled at runtime, so no file in the repo looks like a real
credential to a push-protection scanner.
"""

import logging
import time

import pytest
from fakes import FAKE_KEY, template_config

from arpeggio_ai.safety import secret_scan
from arpeggio_ai.safety.secret_scan import (
    SecretScanner,
    current_scanner,
    is_expression,
    scanner_from_config,
    shannon_entropy,
    use_scanner,
)

AWS = "AKIA" + "Q7RZ3MX9KD2LPW5T"
GITHUB = "gh" + "p_" + "a1B2c3D4e5F6g7H8i9J0" * 2
GITHUB_PAT = "github" + "_pat_" + "11ABCDEFG0" * 7
SLACK = "xo" + "xb-" + "1234567890-abcdefghij"
GOOGLE = "AI" + "za" + "Sy" + "D3fG7hJ9kL2mN4pQ6rS8tU0vW1xY3zA5b"
OPENAI = "s" + "k-" + "proj-4fT9qL2mZ8xR7wK3vB6n"
GROQ = "gs" + "k_" + "Q2w9E8r7T6y5U4i3O2p1A0s9D8f7G6h5J4k3L2z1"
JWT = (
    "ey" + "JhbGciOiJIUzI1NiJ9.ey" + "JzdWIiOiIxMjM0In0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"
)
PRIVATE_KEY = (
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA7\nq2w3e4r5t6\n-----END RSA PRIVATE KEY-----"
)


def scan(text: str, scanner: SecretScanner | None = None) -> tuple[str, list[str]]:
    result = (scanner or SecretScanner()).scan(text, "test")
    return result.text, [finding.type for finding in result.findings]


# Detectors: one positive and one negative each


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("aws_access_key", AWS),
        ("github_token", GITHUB),
        ("github_token", GITHUB_PAT),
        ("slack_token", SLACK),
        ("google_api_key", GOOGLE),
        ("openai_style_key", OPENAI),
        ("groq_key", GROQ),
        ("jwt", JWT),
        ("private_key", PRIVATE_KEY),
    ],
)
def test_detector_finds_and_redacts(kind: str, value: str) -> None:
    text, kinds = scan(f"before {value} after\n")
    assert kinds == [kind]
    assert text == f"before [REDACTED:{kind}] after\n"


@pytest.mark.parametrize(
    "text",
    [
        "AKIA" + "Q7RZ3MX9",  # too short
        "XAKIA" + "Q7RZ3MX9KD2LPW5T",  # starts inside a longer word
        "gh" + "p_" + "short123",
        "gh" + "x_" + "a1B2c3D4e5F6g7H8i9J0" * 2,  # unknown prefix letter
        "xo" + "xz-1234567890-abc",
        "AI" + "za" + "tooShort",
        "risk-assessment-for-the-new-module-layout",  # kebab-case that contains "sk-"
        "s" + "k-short",
        "gs" + "k_" + "tooshort",
        "eyJhbGciOi.eyJzdWIi.abc",  # segments too short
        "-----BEGIN PUBLIC KEY-----\nMIIBIjANBg\n-----END PUBLIC KEY-----",
    ],
)
def test_detector_ignores_lookalikes(text: str) -> None:
    assert scan(text) == (text, [])


def test_private_key_without_end_line_is_redacted_to_the_end() -> None:
    text, kinds = scan("x = 1\n-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1r\nmore")
    assert (text, kinds) == ("x = 1\n[REDACTED:private_key]", ["private_key"])


def test_two_private_keys_are_two_findings() -> None:
    _, kinds = scan(f"{PRIVATE_KEY}\nmiddle\n{PRIVATE_KEY}\n")
    assert kinds == ["private_key", "private_key"]


def test_findings_carry_type_source_and_line_but_no_value() -> None:
    result = SecretScanner().scan(f"one\ntwo {AWS}\nthree\n{PRIVATE_KEY}\nend {GITHUB}", "ctx")
    assert [(f.type, f.source, f.line) for f in result.findings] == [
        ("aws_access_key", "ctx", 2),
        ("private_key", "ctx", 4),
        ("github_token", "ctx", 8),
    ]
    assert result.counts() == {"aws_access_key": 1, "github_token": 1, "private_key": 1}
    assert AWS not in repr(result)


# assigned_secret


HIGH = "q8Zr2LmX9vT4nB7cW1yK"  # 20 distinct characters, 4.32 bits/char


@pytest.mark.parametrize(
    "line",
    [
        f"api_key = {HIGH}",
        f'API_KEY="{HIGH}"',
        f"db_password: '{HIGH}'",
        f'"client_secret": "{HIGH}"',
        f"GITHUB_TOKEN={HIGH}",
        f"access-key := {HIGH}",
        f"'secret' => '{HIGH}',",
    ],
)
def test_assigned_secret_redacts_only_the_value(line: str) -> None:
    text, kinds = scan(line)
    assert kinds == ["assigned_secret"]
    assert HIGH not in text
    assert text == line.replace(HIGH, "[REDACTED:assigned_secret]")


def test_entropy_threshold_boundary() -> None:
    exactly = "abcdefgh" + "ijklijkl"  # 8 chars once, 4 chars twice: exactly 3.5 bits/char
    below = "abcdefg" + "hijkhijkh"
    assert shannon_entropy(exactly) == 3.5
    assert shannon_entropy(below) < 3.5
    assert scan(f"token = {exactly}")[1] == ["assigned_secret"]
    assert scan(f"token = {below}")[1] == []


def test_assigned_secret_needs_16_characters() -> None:
    assert scan("password = q8Zr2LmX9vT4nB7")[1] == []  # 15 characters
    assert scan("password = q8Zr2LmX9vT4nB7c")[1] == ["assigned_secret"]


@pytest.mark.parametrize(
    "value",
    [
        "<your-api-key-goes-here>",
        "${OPENAI_API_KEY_VALUE}",
        "env:OPENAI_API_KEY_VALUE",
        "keychain:openai-production",
        "aaaaaaaaaaaaaaaaaaaa",
        "XXXX-XXXX-XXXX-XXXX-XX",
        "Q9w8****************",
        "'{{ api_key_from_vault }}'",
    ],
)
def test_placeholders_are_ignored(value: str) -> None:
    assert scan(f"api_key = {value}")[1] == []
    assert scan(f'api_key = "{value}"')[1] == []


@pytest.mark.parametrize(
    "line",
    [
        "$token = $request->input('reset_token_value');",
        "'password' => Hash::make($request->password),",
        '<input type="hidden" name="_token" value="{{ csrf_token() }}">',
        "'secret' => env('STRIPE_SECRET'),",
        "api_key = settings.OPENAI_API_KEY",
        "secret_key = os.environ['DJANGO_SECRET_KEY']",
        "token = @user.reset_password_token_value",
        "client_secret = {{ vault.client_secret_value }}",
        "password = getPasswordFromVaultService(name)",
    ],
)
def test_code_expressions_are_not_secrets(line: str) -> None:
    assert scan(line) == (line, [])


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("$request->input", True),
        ("@config.secret", True),
        ("Hash::make($x)", True),
        ("getenv(X)", True),
        ("config.api_key", True),
        ("settings.SECRET_KEY", True),
        ("os.environ['X']", True),
        ("q8Zr2LmX9vT4nB7cW1yK", False),  # one bare word: how .env and YAML hold secrets
        ("sk_live_q8Zr2LmX9vT4nB7c", False),
    ],
)
def test_is_expression(value: str, expected: bool) -> None:
    assert is_expression(value) is expected


def test_quoted_literal_that_looks_like_code_is_still_checked() -> None:
    assert scan("password = 'Pa$$(w0rd)->Xy9::Kq7z'")[1] == ["assigned_secret"]


def test_type_annotation_is_not_an_assignment() -> None:
    assert scan("def f(token: str, api_key: Optional[str] = None): ...")[1] == []


# configured_key


def test_configured_key_is_redacted_with_highest_priority() -> None:
    scanner = SecretScanner(configured=[FAKE_KEY])
    text, kinds = scan(f"key={FAKE_KEY} and api_key = {FAKE_KEY}", scanner)
    assert kinds == ["configured_key", "configured_key"]
    assert FAKE_KEY not in text


def test_configured_key_shorter_than_8_characters_is_ignored() -> None:
    scanner = SecretScanner(configured=["short1", "", "longer12"])
    assert scan("short1 longer12", scanner) == (
        "short1 [REDACTED:configured_key]",
        ["configured_key"],
    )


def test_scanner_repr_never_shows_configured_values() -> None:
    scanner = SecretScanner(configured=[FAKE_KEY], allow=["x"])
    assert FAKE_KEY not in repr(scanner)
    assert repr(scanner) == "SecretScanner(configured=1, allow=1)"


def test_scanner_from_config_reads_only_set_provider_variables() -> None:
    config = template_config()
    assert repr(scanner_from_config(config, {})) == "SecretScanner(configured=0, allow=0)"
    scanner = scanner_from_config(config, {"DEEPSEEK_API_KEY": FAKE_KEY, "OTHER": "x" * 20})
    assert repr(scanner) == "SecretScanner(configured=1, allow=0)"
    assert scan(FAKE_KEY, scanner)[1] == ["configured_key"]


# Allowlist


def test_allowlisted_finding_is_kept() -> None:
    scanner = SecretScanner(allow=[r"AKIA" + "Q7RZ3MX9KD2LPW5T"])
    assert scan(f"aws {AWS}", scanner) == (f"aws {AWS}", [])


def test_allowlist_must_match_the_whole_finding() -> None:
    scanner = SecretScanner(allow=["AKIA"])
    assert scan(AWS, scanner)[1] == ["aws_access_key"]


def test_configured_and_private_keys_cannot_be_allowlisted() -> None:
    scanner = SecretScanner(configured=[FAKE_KEY], allow=[".*", "(?s).*"])
    _, kinds = scan(f"{FAKE_KEY}\n{PRIVATE_KEY}\n{AWS}", scanner)
    assert kinds == ["configured_key", "private_key"]


def test_allowlist_comes_from_repo_config() -> None:
    config = template_config()
    config = config.model_copy(
        update={"repo": config.repo.model_copy(update={"secret_scan_allow": [r"AKIA\w+"]})}
    )
    assert scan(AWS, scanner_from_config(config, {}))[1] == []


# Overlaps and chunking


def test_overlapping_findings_keep_the_higher_priority() -> None:
    _, kinds = scan(f"api_key = {OPENAI}")
    assert kinds == ["openai_style_key"]


def test_secret_across_a_chunk_border_is_found(monkeypatch: pytest.MonkeyPatch) -> None:
    border = secret_scan.CHUNK_CHARS
    filler = "a b " * (border // 4)
    text = filler[: border - 10] + " " + GITHUB + " " + filler[:2000]
    assert len(text) > border
    result = SecretScanner().scan(text, "big")
    assert [f.type for f in result.findings] == ["github_token"]
    assert GITHUB not in result.text


def test_private_key_longer_than_the_overlap_across_a_border_is_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(secret_scan, "CHUNK_CHARS", 4096)
    body = "\n".join(["MIIEowIBAAKCAQEA7q2w3e4r5t6y7u8i9o0p"] * 200)  # about 7 KiB
    key = f"-----BEGIN PRIVATE KEY-----\n{body}\n-----END PRIVATE KEY-----"
    text = "x" * 4000 + key + "y" * 5000
    result = SecretScanner().scan(text, "big")
    assert [f.type for f in result.findings] == ["private_key"]
    assert result.text == "x" * 4000 + "[REDACTED:private_key]" + "y" * 5000


def test_token_found_in_two_windows_is_reported_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secret_scan, "CHUNK_CHARS", 1000)
    text = "z " * 495 + AWS + " z" * 2000  # inside the overlap of the first two windows
    result = SecretScanner().scan(text, "big")
    assert [f.type for f in result.findings] == ["aws_access_key"]


# Logging and the active scanner


def test_redact_logs_counts_and_source_only(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="arpeggio_ai.safety.secret_scan"):
        text = SecretScanner().redact(f"{AWS} {AWS} {GITHUB}", "context:a.py")
    assert AWS not in text
    [record] = caplog.records
    assert record.getMessage() == "secret_scan.redacted"
    assert record.__dict__["source"] == "context:a.py"
    assert record.__dict__["types"] == {"aws_access_key": 2, "github_token": 1}
    assert record.__dict__["redacted"] == 3
    assert AWS not in str(record.__dict__)


def test_redact_logs_nothing_when_clean(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        assert SecretScanner().redact("nothing here", "x") == "nothing here"
    assert caplog.records == []


def test_use_scanner_installs_and_restores() -> None:
    default = current_scanner()
    mine = SecretScanner(configured=[FAKE_KEY])
    with use_scanner(mine):
        assert current_scanner() is mine
    assert current_scanner() is default


@pytest.mark.parametrize(
    "text",
    [
        "token" * 400_000,  # one 2 MB word full of key names
        "api_key=" * 250_000,
        "a=[" * 600_000,
        "-----BEGIN PRIVATE KEY-----" * 70_000,
        "sk-" * 600_000,
    ],
    ids=["key-name-word", "assignments", "open-brackets", "begin-markers", "sk-prefixes"],
)
def test_adversarial_input_scans_in_linear_time(text: str) -> None:
    started = time.perf_counter()
    SecretScanner().scan(text, "adversarial")
    assert time.perf_counter() - started < 5.0
