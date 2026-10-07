from typing import Any

from arpeggio_ai.config.models import parse_config
from arpeggio_ai.safety.process import provider_key_names, scrubbed_env

SOURCE = {
    "PATH": "/usr/bin",
    "HOME": "/home/me",
    "LANG": "C.UTF-8",
    "DEEPSEEK_API_KEY": "sk-secret",
    "AWS_SECRET_ACCESS_KEY": "aws-secret",
    "DATABASE_URL": "sqlite://",
    "SystemRoot": r"C:\Windows",
    "Path": r"C:\bin",
}


def test_only_the_allowlist_passes_by_default() -> None:
    env = scrubbed_env(source=SOURCE, windows=False)
    assert env == {"PATH": "/usr/bin", "HOME": "/home/me", "LANG": "C.UTF-8"}


def test_extra_names_pass_but_denied_names_never_do() -> None:
    env = scrubbed_env(
        extra=["DATABASE_URL", "DEEPSEEK_API_KEY"],
        deny=["DEEPSEEK_API_KEY"],
        source=SOURCE,
        windows=False,
    )
    assert "DATABASE_URL" in env and "DEEPSEEK_API_KEY" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env


def test_posix_names_are_case_sensitive() -> None:
    env = scrubbed_env(source=SOURCE, windows=False)
    assert "Path" not in env and "SystemRoot" not in env


def test_windows_names_are_case_insensitive_and_add_system_variables() -> None:
    env = scrubbed_env(
        extra=["database_url"], deny=["deepseek_api_key"], source=SOURCE, windows=True
    )
    assert env["SystemRoot"] == r"C:\Windows"
    assert env["Path"] == r"C:\bin" and env["PATH"] == "/usr/bin"
    assert "DATABASE_URL" in env and "DEEPSEEK_API_KEY" not in env


def test_provider_key_names(config_data: dict[str, Any]) -> None:
    config_data["providers"]["anthropic"]["api_key"] = "keychain:anthropic"
    assert provider_key_names(parse_config(config_data)) == {"DEEPSEEK_API_KEY"}
