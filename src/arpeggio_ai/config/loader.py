"""Read, merge and validate Arpeggio config files."""

import tomllib
from importlib.resources import files
from pathlib import Path
from typing import Any

from arpeggio_ai.config.models import INLINE_KEY_MESSAGE, Config, is_key_reference, parse_config
from arpeggio_ai.core.errors import ConfigError, ConfigIssue
from arpeggio_ai.paths import global_config_path, repo_config_path

# Packaged config templates, one per budget profile (CLI-05). `init` writes DEFAULT_TEMPLATE.
TEMPLATES = ("free", "micro-deepseek", "standard", "pro")
DEFAULT_TEMPLATE = "free"


def template_bytes(name: str) -> bytes:
    """The packaged template ``templates/<name>.toml``, byte for byte."""
    if name not in TEMPLATES:
        raise ValueError(f"unknown template {name!r}")
    return files("arpeggio_ai.config").joinpath("templates", f"{name}.toml").read_bytes()


def template_unknown_data_use(name: str) -> list[str]:
    """Providers in the template whose data_use is "unknown" (the default)."""
    providers = tomllib.loads(template_bytes(name).decode("utf-8")).get("providers", {})
    return [
        key for key, table in providers.items() if table.get("data_use", "unknown") == "unknown"
    ]


def template_env_vars(name: str) -> list[str]:
    """Environment variables the template's providers read their API keys from."""
    providers = tomllib.loads(template_bytes(name).decode("utf-8")).get("providers", {})
    refs = (table.get("api_key", "") for table in providers.values())
    return sorted({ref.removeprefix("env:") for ref in refs if ref.startswith("env:")})


def read_toml(path: Path) -> dict[str, Any]:
    """Parse one TOML file. Syntax errors keep tomllib's line and column."""
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as error:
        message = f"invalid TOML: {error}"
    except UnicodeDecodeError:
        message = "invalid TOML: file is not valid UTF-8"
    except OSError as error:
        message = f"cannot read file: {error.strerror or type(error).__name__}"
    raise ConfigError([ConfigIssue(file=str(path), field=None, message=message)])


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge ``override`` into a copy of ``base``.

    Tables merge recursively. Arrays and scalars from ``override`` replace the base value.
    """
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _inline_key_issues(raw: dict[str, Any], file: str) -> list[ConfigIssue]:
    """Flag inline keys per file, so a key in one file is caught even if another overrides it."""
    providers = raw.get("providers")
    if not isinstance(providers, dict):
        return []
    return [
        ConfigIssue(file=file, field=f"providers.{name}.api_key", message=INLINE_KEY_MESSAGE)
        for name, table in providers.items()
        if isinstance(table, dict) and "api_key" in table and not is_key_reference(table["api_key"])
    ]


def _read_into(path: Path, issues: list[ConfigIssue]) -> dict[str, Any] | None:
    try:
        raw = read_toml(path)
    except ConfigError as error:
        issues.extend(error.issues)
        return None
    issues.extend(_inline_key_issues(raw, str(path)))
    return raw


def load_config(repo: Path | None = None) -> Config:
    """Load the global config, deep-merge the repo config over it if present, and validate.

    Raises ConfigError listing every issue found, each with file, dotted field and message.
    """
    global_path = global_config_path()
    if not global_path.exists():
        raise ConfigError(
            [
                ConfigIssue(
                    file=str(global_path),
                    field=None,
                    message="config file not found. Run `arpeggio init` to create it.",
                )
            ]
        )

    issues: list[ConfigIssue] = []
    global_raw = _read_into(global_path, issues)
    if global_raw is not None and "repo" in global_raw:
        issues.append(
            ConfigIssue(
                file=str(global_path),
                field="repo",
                message="[repo] is only allowed in <repo>/.arpeggio/config.toml",
            )
        )

    repo_raw = None
    if repo is not None:
        repo_dir = repo.expanduser().absolute()
        repo_path = repo_config_path(repo_dir)
        if not repo_dir.is_dir():
            issues.append(
                ConfigIssue(file=str(repo_dir), field=None, message="repo directory not found")
            )
        elif repo_path.exists():
            repo_raw = _read_into(repo_path, issues)
            if repo_raw is not None and "privacy" in repo_raw:
                issues.append(
                    ConfigIssue(
                        file=str(repo_path),
                        field="privacy",
                        message="[privacy] is only allowed in the global config. "
                        "Use allow_training_providers under [repo] instead.",
                    )
                )
            if repo_raw is not None and "evals" in repo_raw:
                issues.append(
                    ConfigIssue(
                        file=str(repo_path),
                        field="evals",
                        message="[evals] is only allowed in the global config",
                    )
                )

    if issues or global_raw is None:
        raise ConfigError(issues)
    if repo_raw is None:
        return parse_config(global_raw, file=str(global_path))
    return parse_config(deep_merge(global_raw, repo_raw), file="merged")


def load_config_if_present(repo: Path | None = None) -> Config | None:
    """``load_config``, or None when there is no global config yet (commands that work
    without one, such as ``eval`` and ``secrets scan``, fall back to defaults)."""
    if not global_config_path().exists():
        return None
    return load_config(repo)
