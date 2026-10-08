"""Strategy route sequences (EVL-03), table-driven across profiles and tasks."""

from pathlib import Path
from typing import Any

import pytest
from routekit import build_config

from arpeggio_ai.config.models import Config
from arpeggio_ai.evals.strategies import Strategy, plan_routes
from arpeggio_ai.evals.task import EvalTask
from arpeggio_ai.routing.policy import load_policy
from arpeggio_ai.routing.resolve import NoAllowedRoute

POLICY, _ = load_policy(Path("does-not-exist"))

# micro-deepseek shape: tier1 low only, tier2 medium/high, tier3 high/max
MICRO = build_config()
# a free profile with two tiers only
FREE = build_config(
    {"tier1.fast": ("g", 0, 0, ["low"]), "tier2.default": ("g", 0, 0, ["low", "high"])},
    providers={"g": "unknown"},
    profile="free",
)
# standard shape with several tier-2 models
STANDARD = build_config(
    {
        "tier1.cheap": ("d", 1.0, 0.3, ["low", "medium"]),
        "tier2.mid": ("a", 15.0, 3.0, ["low", "medium", "high"]),
        "tier2.alt": ("d", 2.0, 0.5, ["medium"]),
        "tier3.frontier": ("a", 75.0, 15.0, ["medium", "high", "max"]),
    },
    providers={"a": "unknown", "d": "unknown"},
    profile="standard",
)


def task(category: str = "bugfix", risk: str = "low") -> EvalTask:
    return EvalTask.model_validate(
        {
            "id": "t-one",
            "category": category,
            "expected_risk": risk,
            "repo": {"path": "C:/repo" if Path("C:/").exists() else "/repo", "base": "abc1234"},
            "request": "Do the thing properly please.",
            "done_criteria": [{"kind": "command", "argv": ["true"]}],
            "reference_diff": "fixtures/x.diff",
        }
    )


def seq(strategy: Strategy, config: Config, **kwargs: Any) -> list[tuple[str, str]]:
    return [(r.model, r.effort) for r in plan_routes(strategy, task(**kwargs), config, POLICY)]


CASES: list[tuple[Strategy, Config, dict[str, str], list[tuple[str, str]]]] = [
    ("junior", MICRO, {}, [("tier3.pro", "max")]),
    ("junior", FREE, {}, [("tier2.default", "high")]),
    ("junior", STANDARD, {}, [("tier3.frontier", "max")]),
    ("middle", MICRO, {}, [("tier2.flash", "high")]),
    ("middle", MICRO, {"category": "docs"}, [("tier1.flash", "low")]),
    ("middle", MICRO, {"risk": "high"}, [("tier3.pro", "high")]),
    ("middle", STANDARD, {}, [("tier2.alt", "medium")]),  # cheapest tier2, nearest effort
    ("middle", FREE, {"category": "config"}, [("tier1.fast", "low")]),
    (
        "senior",
        MICRO,
        {},
        [("tier1.flash", "low"), ("tier2.flash", "medium"), ("tier3.pro", "high")],
    ),
    (
        "senior",
        FREE,
        {},
        [("tier1.fast", "low"), ("tier2.default", "low"), ("tier2.default", "high")],
    ),
    (
        "senior",
        STANDARD,
        {"risk": "high"},
        [("tier1.cheap", "low"), ("tier2.alt", "medium"), ("tier3.frontier", "high")],
    ),
    (
        "arpeggio",
        MICRO,
        {},
        [("tier2.flash", "medium"), ("tier2.flash", "high"), ("tier3.pro", "high")],
    ),
    ("arpeggio", MICRO, {"category": "docs"}, [("tier1.flash", "low"), ("tier2.flash", "medium")]),
    ("arpeggio", MICRO, {"risk": "medium"}, [("tier2.flash", "medium"), ("tier3.pro", "high")]),
    ("arpeggio", MICRO, {"risk": "high"}, [("tier3.pro", "high")]),
    # tier2 low -> nearest effort is low on FREE; the next step low->high is a new route
    ("arpeggio", FREE, {}, [("tier2.default", "low"), ("tier2.default", "high")]),
]


@pytest.mark.parametrize(("strategy", "config", "kwargs", "expected"), CASES)
def test_route_sequences(
    strategy: Strategy, config: Config, kwargs: dict[str, str], expected: list[tuple[str, str]]
) -> None:
    assert seq(strategy, config, **kwargs) == expected


def test_identical_consecutive_routes_are_merged() -> None:
    one = build_config({"tier2.only": ("d", 1.0, 1.0, ["high"])}, providers={"d": "unknown"})
    assert seq("senior", one) == [("tier2.only", "high")]
    assert seq("arpeggio", one) == [("tier2.only", "high")]


def test_reasons_record_strategy_step_rule_and_requested_effort() -> None:
    routes = plan_routes("arpeggio", task(), MICRO, POLICY)
    assert [r.reason["rule"] for r in routes] == ["low-risk-code"] * 3
    assert [r.reason["step"] for r in routes] == [0, 1, 2]
    assert routes[0].reason["requested_effort"] == "low"
    assert routes[0].effort == "medium"
    assert all(r.reason["strategy"] == "arpeggio" and r.reason["mode"] == "eval" for r in routes)


def test_middle_override_from_config() -> None:
    config = build_config(evals={"middle": {"bugfix": "tier3.pro", "docs": "tier2.flash"}})
    assert seq("middle", config) == [("tier3.pro", "high")]
    assert seq("middle", config, category="docs") == [("tier2.flash", "high")]
    assert seq("middle", config, category="feature") == [("tier2.flash", "high")]


def test_middle_override_to_a_disallowed_model_skips_instead_of_falling_back() -> None:
    config = build_config(
        {"tier1.safe": ("s", 1, 1, ["low"]), "tier2.open": ("o", 2, 1, ["high"])},
        providers={"s": "no_training", "o": "unknown"},
        repo={"privacy_class": "private"},
        evals={"middle": {"bugfix": "tier2.open"}},
    )
    with pytest.raises(NoAllowedRoute, match=r"\[evals.middle\] bugfix"):
        plan_routes("middle", task(), config, POLICY)
    # the other strategies use only the allowed model
    assert seq("junior", config) == [("tier1.safe", "low")]


@pytest.mark.parametrize("strategy", ["junior", "middle", "senior", "arpeggio"])
def test_no_allowed_model_raises_for_every_strategy(strategy: Strategy) -> None:
    config = build_config(repo={"privacy_class": "client"}, privacy=True)
    with pytest.raises(NoAllowedRoute, match="client repo"):
        plan_routes(strategy, task(), config, POLICY)
