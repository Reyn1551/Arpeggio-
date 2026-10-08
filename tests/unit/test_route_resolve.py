"""Route resolution (M0.6 §4.2): allowed models, tiers, efforts and SAF-07."""

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from routekit import build_config

from arpeggio_ai.config.models import ModelSpec
from arpeggio_ai.routing.resolve import (
    EFFORT_ORDER,
    NoAllowedRoute,
    ResolvedRoute,
    allowed_models,
    cheapest,
    highest_effort,
    nearest_effort,
    nearest_tier,
    require_allowed,
    resolve_tier,
    strongest,
)

MODELS = {
    "tier1.b": ("p", 1.0, 0.5, ["low"]),
    "tier1.a": ("p", 1.0, 0.5, ["low", "medium"]),  # same prices as b: key name decides
    "tier1.pricey": ("p", 2.0, 0.1, ["low"]),
    "tier3.x": ("p", 5.0, 1.0, ["high"]),
    "tier3.y": ("p", 9.0, 1.0, ["medium", "max"]),
}


def spec(efforts: list[str]) -> ModelSpec:
    config = build_config({"tier1.m": ("p", 1.0, 1.0, efforts)}, providers={"p": "unknown"})
    return config.models["tier1.m"]


def test_cheapest_breaks_ties_by_input_price_then_key() -> None:
    models = allowed_models(build_config(MODELS, providers={"p": "unknown"}))
    assert cheapest(models, 1) == "tier1.a"
    tied = build_config(
        {"tier1.b": ("p", 1.0, 0.4, ["low"]), "tier1.a": ("p", 1.0, 0.5, ["low"])},
        providers={"p": "unknown"},
    )
    assert cheapest(allowed_models(tied), 1) == "tier1.b"


def test_strongest_is_highest_tier_then_highest_output_price() -> None:
    models = allowed_models(build_config(MODELS, providers={"p": "unknown"}))
    assert strongest(models) == "tier3.y"


def test_nearest_tier_prefers_lower_on_a_tie() -> None:
    models = allowed_models(build_config(MODELS, providers={"p": "unknown"}))
    assert [nearest_tier(models, n) for n in (1, 2, 3)] == [1, 1, 3]
    only3 = {k: v for k, v in models.items() if k.startswith("tier3")}
    assert nearest_tier(only3, 1) == 3


@pytest.mark.parametrize(
    ("efforts", "requested", "expected"),
    [
        (["low"], "high", "low"),
        (["medium", "high"], "low", "medium"),
        (["low", "high"], "medium", "low"),  # equidistant: lower wins
        (["high", "max"], "medium", "high"),
        (["low", "max"], "high", "max"),
        (["medium"], "medium", "medium"),
    ],
)
def test_nearest_effort(efforts: list[str], requested: str, expected: str) -> None:
    assert nearest_effort(spec(efforts), requested) == expected  # type: ignore[arg-type]


@given(
    st.lists(st.sampled_from(EFFORT_ORDER), min_size=1, max_size=4, unique=True),
    st.sampled_from(EFFORT_ORDER),
)
def test_nearest_effort_is_allowed_and_closest(efforts: list[str], requested: str) -> None:
    model = spec(efforts)
    got = nearest_effort(model, requested)  # type: ignore[arg-type]
    assert got in efforts
    distance = abs(EFFORT_ORDER.index(got) - EFFORT_ORDER.index(requested))  # type: ignore[arg-type]
    assert all(
        abs(EFFORT_ORDER.index(e) - EFFORT_ORDER.index(requested)) >= distance  # type: ignore[arg-type]
        for e in efforts
    )
    assert highest_effort(model) == max(efforts, key=EFFORT_ORDER.index)  # type: ignore[arg-type]


@given(
    st.dictionaries(
        st.tuples(st.integers(1, 3), st.sampled_from("abcd")),
        st.tuples(st.floats(0, 10, allow_nan=False), st.floats(0, 10, allow_nan=False)),
        min_size=1,
        max_size=6,
    )
)
def test_tier_choices_stay_inside_the_allowed_set(rows: dict[Any, Any]) -> None:
    models = {f"tier{t}.{n}": ("p", out, inp, ["low"]) for (t, n), (out, inp) in rows.items()}
    allowed = allowed_models(build_config(models, providers={"p": "unknown"}))
    top = strongest(allowed)
    assert int(top[4]) == max(int(k[4]) for k in allowed)
    for tier in (1, 2, 3):
        chosen = resolve_tier(allowed, tier, "low")
        assert chosen.model in allowed
        same_tier = [k for k in allowed if k[4] == chosen.model[4]]
        assert all(
            (allowed[k].price_out_per_m, allowed[k].price_in_per_m)
            >= (
                allowed[chosen.model].price_out_per_m,
                allowed[chosen.model].price_in_per_m,
            )
            for k in same_tier
        )


def test_resolve_tier_records_requested_and_effective_effort() -> None:
    models = allowed_models(build_config())
    assert resolve_tier(models, 2, "low") == ResolvedRoute("tier2.flash", "medium", "low")
    assert resolve_tier(models, "strongest", "high") == ResolvedRoute("tier3.pro", "high", "high")


# Filters


def test_free_profile_allows_only_free_models() -> None:
    config = build_config(profile="free")
    assert set(allowed_models(config)) == set(config.models)


def test_placeholders_are_never_allowed() -> None:
    config = build_config(placeholders=("tier3.pro",))
    assert "tier3.pro" not in allowed_models(config)
    with pytest.raises(NoAllowedRoute, match="placeholder"):
        require_allowed(build_config(placeholders=("tier1.flash", "tier2.flash", "tier3.pro")))


TWO = {
    "tier1.open": ("open", 1.0, 1.0, ["low"]),
    "tier3.safe": ("safe", 5.0, 1.0, ["high"]),
}
PROVIDERS = {"open": "unknown", "safe": "no_training"}


@pytest.mark.parametrize(
    ("repo", "privacy", "expected"),
    [
        ({"privacy_class": "public"}, None, {"tier1.open", "tier3.safe"}),
        ({"privacy_class": "private"}, None, {"tier3.safe"}),
        ({"privacy_class": "private"}, True, {"tier1.open", "tier3.safe"}),
        ({"privacy_class": "private", "allow_training_providers": False}, True, {"tier3.safe"}),
        (
            {"privacy_class": "private", "allow_training_providers": True},
            False,
            {"tier1.open", "tier3.safe"},
        ),
        ({"privacy_class": "client"}, True, {"tier3.safe"}),  # global opt-in ignored
        (
            {"privacy_class": "client", "allow_training_providers": True},
            None,
            {"tier1.open", "tier3.safe"},
        ),
        ({"privacy_class": "public", "provider_allow": ["open"]}, None, {"tier1.open"}),
    ],
)
def test_saf07_filters_providers(
    repo: dict[str, Any], privacy: bool | None, expected: set[str]
) -> None:
    config = build_config(TWO, providers=PROVIDERS, repo=repo, privacy=privacy)
    assert set(allowed_models(config)) == expected


def test_no_allowed_route_explains_the_opt_in() -> None:
    config = build_config(repo={"privacy_class": "private"})
    with pytest.raises(NoAllowedRoute) as raised:
        require_allowed(config)
    message = str(raised.value)
    assert "private repo" in message
    assert "deep (unknown)" in message
    assert "allow_training_providers = true" in message
    assert "[privacy]" in message
    client = build_config(repo={"privacy_class": "client"})
    with pytest.raises(NoAllowedRoute) as raised:
        require_allowed(client)
    assert "[privacy]" not in str(raised.value)
