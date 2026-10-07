import pytest

from arpeggio_ai.core.errors import SecretNotFound
from arpeggio_ai.core.secrets import reference_name, resolve

FAKE = "sk-fake-0123456789abcdef"


def test_env_reference_resolves() -> None:
    secret = resolve("env:DEEPSEEK_API_KEY", {"DEEPSEEK_API_KEY": FAKE})
    assert secret.get_secret_value() == FAKE


def test_reads_the_process_environment_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARPEGGIO_TEST_KEY", FAKE)
    assert resolve("env:ARPEGGIO_TEST_KEY").get_secret_value() == FAKE


@pytest.mark.parametrize("env", [{}, {"DEEPSEEK_API_KEY": ""}])
def test_unset_or_empty_variable_names_only_the_variable(env: dict[str, str]) -> None:
    with pytest.raises(SecretNotFound) as caught:
        resolve("env:DEEPSEEK_API_KEY", env)
    assert str(caught.value) == "environment variable DEEPSEEK_API_KEY is not set"


def test_keychain_is_not_supported_yet() -> None:
    with pytest.raises(NotImplementedError, match="keychain references are not supported yet"):
        resolve("keychain:deepseek", {})


@pytest.mark.parametrize("ref", [FAKE, "env:", "vault:KEY", "env:BAD NAME"])
def test_malformed_reference_is_not_echoed(ref: str) -> None:
    with pytest.raises(ValueError) as caught:
        resolve(ref, {})
    assert FAKE not in str(caught.value)
    assert str(caught.value) == "secret reference must look like env:NAME"


def test_value_never_shows_in_repr_or_str() -> None:
    secret = resolve("env:K", {"K": FAKE})
    assert FAKE not in repr(secret)
    assert FAKE not in str(secret)
    assert FAKE not in f"{secret}"


def test_reference_name() -> None:
    assert reference_name("env:DEEPSEEK_API_KEY") == "DEEPSEEK_API_KEY"
    assert reference_name(FAKE) == "(invalid reference)"
