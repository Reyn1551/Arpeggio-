"""Routing policy file (RTE-04): default, first match, override, invalid files."""

from pathlib import Path

import pytest

from arpeggio_ai.core.errors import ConfigError
from arpeggio_ai.routing.policy import DEFAULT_SOURCE, load_policy, parse_policy


def test_default_policy_validates_and_matches_first_rule(tmp_path: Path) -> None:
    policy, source = load_policy(tmp_path)
    assert source == DEFAULT_SOURCE
    cases = {
        ("feature", "high"): "high-risk",
        ("docs", "high"): "high-risk",  # first match wins over docs-and-config
        ("docs", "low"): "docs-and-config",
        ("config", "low"): "docs-and-config",
        ("bugfix", "low"): "low-risk-code",
        ("docs", "medium"): "medium-default",
        ("feature", "unknown"): "fallback",
    }
    for (category, risk), rule_id in cases.items():
        rule = policy.match(category, risk)
        assert rule is not None and rule.id == rule_id
    low = policy.match("bugfix", "low")
    assert low is not None
    assert [(r.tier, r.effort) for r in [low.route, *low.escalation]] == [
        (2, "low"),
        (2, "high"),
        ("strongest", "high"),
    ]


def test_override_file_replaces_the_default(tmp_path: Path) -> None:
    (tmp_path / "policy.yaml").write_text(
        "version: 1\nrules:\n  - id: only\n    when: {category: docs}\n"
        "    route: {tier: 3, effort: max}\n",
        encoding="utf-8",
    )
    policy, source = load_policy(tmp_path)
    assert source == str(tmp_path / "policy.yaml")
    rule = policy.match("docs", "low")
    assert rule is not None and rule.id == "only" and rule.escalation == []
    assert policy.match("feature", "low") is None


@pytest.mark.parametrize(
    ("text", "field"),
    [
        ("version: 2\nrules: [{id: a, when: {}, route: {tier: 1, effort: low}}]", "version"),
        ("version: 1\nrules: []", "rules"),
        ("version: 1\nrules: [{id: a, when: {}, route: {tier: 4, effort: low}}]", "rules.0"),
        ("version: 1\nrules: [{id: a, when: {}, route: {tier: 1, effort: huge}}]", "rules.0"),
        ("version: 1\nrules: [{id: a, when: {}, route: {model: tier1.x, effort: low}}]", "rules.0"),
        (
            "version: 1\nrules: [{id: a, when: {colour: red}, route: {tier: 1, effort: low}}]",
            "rules.0",
        ),
        (
            "version: 1\nrules: [{id: a, when: {category: cooking},"
            " route: {tier: 1, effort: low}}]",
            "rules.0",
        ),
        (
            "version: 1\nrules: [{id: a, when: {}, route: {tier: 1, effort: low}, escalation:"
            " [{tier: 1, effort: low}, {tier: 2, effort: low}, {tier: 3, effort: low}]}]",
            "rules.0.escalation",
        ),
        (
            "version: 1\nrules: [{id: a, when: {}, route: {tier: 1, effort: low}},"
            " {id: a, when: {}, route: {tier: 1, effort: low}}]",
            None,
        ),
    ],
)
def test_invalid_policy_is_a_config_error(text: str, field: str | None) -> None:
    with pytest.raises(ConfigError) as raised:
        parse_policy(text, "p.yaml")
    issues = raised.value.issues
    assert issues and all(issue.file == "p.yaml" for issue in issues)
    if field is not None:
        assert any((issue.field or "").startswith(field) for issue in issues)


def test_broken_yaml_override_is_a_config_error(tmp_path: Path) -> None:
    (tmp_path / "policy.yaml").write_text("version: [1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_policy(tmp_path)
