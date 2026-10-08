import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest

from arpeggio_ai.config.loader import (
    TEMPLATES,
    deep_merge,
    load_config,
    read_toml,
    template_bytes,
    template_env_vars,
)
from arpeggio_ai.config.models import INLINE_KEY_MESSAGE, parse_config
from arpeggio_ai.core.errors import ConfigError, ConfigIssue

WriteFile = Callable[[str], Path]


def load_issues(repo: Path | None = None) -> list[ConfigIssue]:
    with pytest.raises(ConfigError) as exc_info:
        load_config(repo)
    return exc_info.value.issues


@pytest.mark.parametrize(
    ("name", "env_vars"),
    [
        ("free", ["GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY"]),
        ("micro-deepseek", ["DEEPSEEK_API_KEY"]),
        ("standard", ["ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY"]),
        ("pro", ["ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"]),
    ],
)
def test_template_env_vars(name: str, env_vars: list[str]) -> None:
    assert template_env_vars(name) == env_vars


def test_templates_are_the_four_profiles() -> None:
    assert TEMPLATES == ("free", "micro-deepseek", "standard", "pro")
    with pytest.raises(ValueError):
        template_bytes("config.example")


def test_template_bytes_is_the_packaged_file(template_text: str) -> None:
    text = template_bytes("standard").decode("utf-8")
    # Compare lines: a Windows checkout with core.autocrlf stores the template with CRLF.
    assert text.splitlines() == template_text.splitlines()
    parse_config(tomllib.loads(text))


def test_loads_global_config(write_global: WriteFile, template_text: str) -> None:
    write_global(template_text)
    config = load_config()
    assert config.budget.per_task_usd == 2.0


# Missing global config (case 16)


def test_missing_global_config_points_to_init(home: Path) -> None:
    issues = load_issues()
    assert len(issues) == 1
    assert issues[0].file == str(home / "config.toml")
    assert issues[0].field is None
    assert "arpeggio init" in issues[0].message


# TOML problems (case 12)


def test_toml_syntax_error_reports_line_and_column(write_global: WriteFile) -> None:
    path = write_global("[budget]\nper_task_usd = \n")
    issues = load_issues()
    assert len(issues) == 1
    assert issues[0].file == str(path)
    assert issues[0].field is None
    assert issues[0].message.startswith("invalid TOML:")
    assert "line 2" in issues[0].message
    assert "column" in issues[0].message


def test_non_utf8_file_is_a_config_error(home: Path) -> None:
    home.mkdir()
    (home / "config.toml").write_bytes(b"\xff\xfe[budget]")
    assert load_issues()[0].message == "invalid TOML: file is not valid UTF-8"


def test_unreadable_config_is_a_config_error(home: Path) -> None:
    (home / "config.toml").mkdir(parents=True)
    assert load_issues()[0].message.startswith("cannot read file:")


def test_read_toml_returns_dict(tmp_path: Path) -> None:
    path = tmp_path / "x.toml"
    path.write_text("a = { b = [1, 2] }\n", encoding="utf-8")
    assert read_toml(path) == {"a": {"b": [1, 2]}}


# Merge (case 9)


def test_repo_config_overrides_scalar_replaces_array_and_merges_tables(
    write_global: WriteFile, write_repo: WriteFile, template_text: str, repo_dir: Path
) -> None:
    write_global(template_text)
    write_repo(
        "[budget]\n"
        "per_task_usd = 1.5\n"
        '[models."tier2.mid"]\n'
        'efforts = ["high"]\n'
        "[providers.deepseek]\n"
        'base_url = "https://example.test/v1"\n'
        "[providers.local]\n"
        'kind = "openai_compatible"\n'
        'base_url = "https://llm.example.test"\n'
        'api_key = "keychain:local-llm"\n'
    )

    config = load_config(repo_dir)

    assert config.budget.per_task_usd == 1.5
    assert config.budget.per_day_usd == 10.0
    assert config.models["tier2.mid"].efforts == ["high"]
    assert config.models["tier2.mid"].model == "<provider-model-id>"
    deepseek = config.providers["deepseek"]
    assert deepseek.base_url == "https://example.test/v1"
    assert deepseek.kind == "openai_compatible"
    assert deepseek.api_key == "env:DEEPSEEK_API_KEY"
    assert set(config.providers) == {"anthropic", "deepseek", "local"}


def test_repo_override_applies_only_to_that_repo(
    write_global: WriteFile, write_repo: WriteFile, template_text: str, repo_dir: Path
) -> None:
    write_global(template_text)
    write_repo("[budget]\nper_task_usd = 1.5\n")
    assert load_config(repo_dir).budget.per_task_usd == 1.5
    assert load_config().budget.per_task_usd == 2.0


def test_repo_without_config_uses_global(
    write_global: WriteFile, template_text: str, repo_dir: Path
) -> None:
    write_global(template_text)
    assert load_config(repo_dir) == load_config()


def test_missing_repo_directory_is_a_config_error(
    write_global: WriteFile, template_text: str, tmp_path: Path
) -> None:
    write_global(template_text)
    issues = load_issues(tmp_path / "nope")
    assert issues == [
        ConfigIssue(file=str(tmp_path / "nope"), field=None, message="repo directory not found")
    ]


def test_deep_merge_does_not_mutate_inputs() -> None:
    base = {"a": {"b": 1, "c": [1]}, "d": 1}
    override = {"a": {"c": [2]}, "e": 2}
    assert deep_merge(base, override) == {"a": {"b": 1, "c": [2]}, "d": 1, "e": 2}
    assert base == {"a": {"b": 1, "c": [1]}, "d": 1}
    assert override == {"a": {"c": [2]}, "e": 2}


def test_table_replaced_by_scalar_and_back() -> None:
    assert deep_merge({"a": {"b": 1}}, {"a": 3}) == {"a": 3}
    assert deep_merge({"a": 3}, {"a": {"b": 1}}) == {"a": {"b": 1}}


# [repo] placement (case 10)


def test_repo_table_in_global_config_is_rejected(
    write_global: WriteFile, template_text: str
) -> None:
    path = write_global(template_text + '\n[repo]\nprivacy_class = "public"\n')
    issues = load_issues()
    assert issues == [
        ConfigIssue(
            file=str(path),
            field="repo",
            message="[repo] is only allowed in <repo>/.arpeggio/config.toml",
        )
    ]


def test_repo_table_in_repo_config_is_accepted(
    write_global: WriteFile, write_repo: WriteFile, template_text: str, repo_dir: Path
) -> None:
    write_global(template_text)
    write_repo('[repo]\nprivacy_class = "client"\nprovider_allow = ["anthropic"]\n')
    repo = load_config(repo_dir).repo
    assert repo.privacy_class == "client"
    assert repo.provider_allow == ["anthropic"]


# Validation issues and file attribution


def test_global_only_issues_name_the_global_file(
    write_global: WriteFile, template_text: str
) -> None:
    path = write_global(template_text.replace("per_day_usd    = 10.00", "per_day_usd = 1.0"))
    assert load_issues() == [
        ConfigIssue(file=str(path), field="budget.per_day_usd", message="must be >= per_task_usd")
    ]


def test_merged_issues_are_labelled_merged(
    write_global: WriteFile, write_repo: WriteFile, template_text: str, repo_dir: Path
) -> None:
    write_global(template_text)
    write_repo('[repo]\nprovider_allow = ["openai"]\n')
    assert load_issues(repo_dir) == [
        ConfigIssue(
            file="merged", field="repo.provider_allow.0", message="unknown provider 'openai'"
        )
    ]


def test_unknown_field_in_repo_config_is_rejected(
    write_global: WriteFile, write_repo: WriteFile, template_text: str, repo_dir: Path
) -> None:
    write_global(template_text)
    write_repo("[budget]\nper_week_usd = 3.0\n")
    assert load_issues(repo_dir) == [
        ConfigIssue(file="merged", field="budget.per_week_usd", message="unknown field")
    ]


# Inline keys per file (case 2)


def test_inline_key_in_global_is_rejected_even_when_repo_overrides_it(
    write_global: WriteFile, write_repo: WriteFile, template_text: str, repo_dir: Path
) -> None:
    path = write_global(template_text.replace("env:ANTHROPIC_API_KEY", "sk-test-123"))
    write_repo('[providers.anthropic]\napi_key = "env:ANTHROPIC_API_KEY"\n')
    with pytest.raises(ConfigError) as exc_info:
        load_config(repo_dir)
    assert exc_info.value.issues == [
        ConfigIssue(file=str(path), field="providers.anthropic.api_key", message=INLINE_KEY_MESSAGE)
    ]
    assert "sk-test-123" not in str(exc_info.value)


def test_inline_key_in_repo_config_is_rejected(
    write_global: WriteFile, write_repo: WriteFile, template_text: str, repo_dir: Path
) -> None:
    write_global(template_text)
    path = write_repo('[providers.deepseek]\napi_key = "sk-test-123"\n')
    with pytest.raises(ConfigError) as exc_info:
        load_config(repo_dir)
    assert exc_info.value.issues == [
        ConfigIssue(file=str(path), field="providers.deepseek.api_key", message=INLINE_KEY_MESSAGE)
    ]
    assert "sk-test-123" not in str(exc_info.value)


def test_global_and_repo_problems_are_reported_together(
    write_global: WriteFile, write_repo: WriteFile, template_text: str, repo_dir: Path
) -> None:
    global_path = write_global(template_text + '\n[repo]\nprivacy_class = "public"\n')
    repo_path = write_repo("[budget\n")
    issues = load_issues(repo_dir)
    assert [(issue.file, issue.field) for issue in issues] == [
        (str(global_path), "repo"),
        (str(repo_path), None),
    ]


def test_privacy_table_is_global_only(
    write_global: WriteFile, write_repo: WriteFile, template_text: str, repo_dir: Path
) -> None:
    write_global(template_text + "\n[privacy]\nallow_training_providers = true\n")
    assert load_config().privacy.allow_training_providers is True
    path = write_repo("[privacy]\nallow_training_providers = true\n")
    issues = load_issues(repo_dir)
    assert [(issue.file, issue.field) for issue in issues] == [(str(path), "privacy")]
    assert issues[0].message.startswith("[privacy] is only allowed in the global config.")


def test_repo_opt_in_overrides_global(
    write_global: WriteFile, write_repo: WriteFile, template_text: str, repo_dir: Path
) -> None:
    write_global(template_text + "\n[privacy]\nallow_training_providers = true\n")
    write_repo("[repo]\nallow_training_providers = false\n")
    assert load_config(repo_dir).allows_training_providers() is False


def test_evals_table_is_global_only(
    write_global: WriteFile,
    write_repo: WriteFile,
    template_text: str,
    repo_dir: Path,
    tmp_path: Path,
) -> None:
    target = (tmp_path / "my-evals").as_posix()
    write_global(template_text + f'\n[evals]\ndir = "{target}"\n')
    assert load_config().evals.dir == target
    path = write_repo(f'[evals]\ndir = "{target}"\n')
    issues = load_issues(repo_dir)
    assert [(issue.file, issue.field) for issue in issues] == [(str(path), "evals")]


def test_evals_dir_must_be_absolute(write_global: WriteFile, template_text: str) -> None:
    write_global(template_text + '\n[evals]\ndir = "relative/evals"\n')
    issues = load_issues(None)
    assert [issue.field for issue in issues] == ["evals.dir"]
