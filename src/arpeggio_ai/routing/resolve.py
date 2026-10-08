"""Route resolution: which models a task may use, and which one a tier or effort means.

A route is ``(model_key, effort)``. Only models allowed for the task's repo are ever
considered (``allowed_models``):

1. the active profile: under ``free`` only ``free = true`` models or loopback providers
   (BUD-02),
2. SAF-07: a ``private`` or ``client`` repo uses only providers whose ``data_use`` is
   ``no_training``, unless the repo's effective opt-in allows the others
   (``Config.allows_training_providers``); ``[repo] provider_allow`` narrows it further,
3. the placeholder guard: a model whose id is still ``<...>`` is never used.

``tierN`` means every allowed model whose key starts with ``tierN.``. Every function here
is pure, so the rules can be property-tested. Nothing ever falls back to a model outside
the allowed set: no allowed model raises ``NoAllowedRoute`` with a message saying how to
opt in.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, get_args

from arpeggio_ai.config.models import Config, Effort, ModelSpec, is_loopback_url
from arpeggio_ai.core.errors import ArpeggioError
from arpeggio_ai.cost.guard import is_placeholder

EFFORT_ORDER: tuple[Effort, ...] = get_args(Effort)
Strongest = Literal["strongest"]


class NoAllowedRoute(ArpeggioError):
    """No model is allowed for this task, or the requested one is not."""


@dataclass(frozen=True, slots=True)
class ResolvedRoute:
    model: str  # model key, e.g. "tier2.flash"
    effort: Effort  # effective effort, one of the model's efforts
    requested_effort: Effort


def tier_of(key: str) -> int:
    return int(key.split(".", 1)[0].removeprefix("tier"))


def provider_allowed(config: Config, provider_name: str) -> bool:
    """SAF-07 for the repo behind ``config`` (the merged global and repo config)."""
    repo = config.repo
    if repo.provider_allow is not None and provider_name not in repo.provider_allow:
        return False
    if repo.privacy_class == "public":
        return True
    if config.providers[provider_name].data_use == "no_training":
        return True
    return config.allows_training_providers()


def model_allowed(config: Config, key: str) -> bool:
    spec = config.models[key]
    provider = config.providers[spec.provider]
    if is_placeholder(spec.model):
        return False
    if config.budget.profile == "free" and not (spec.free or is_loopback_url(provider.base_url)):
        return False
    return provider_allowed(config, spec.provider)


def allowed_models(config: Config) -> dict[str, ModelSpec]:
    return {key: spec for key, spec in config.models.items() if model_allowed(config, key)}


def why_none_allowed(config: Config) -> str:
    """An actionable message for a repo where no model is allowed."""
    repo = config.repo
    placeholders = [k for k, s in config.models.items() if is_placeholder(s.model)]
    if placeholders and len(placeholders) == len(config.models):
        return "every model id is still a placeholder; edit your config"
    blocked = sorted(name for name in config.providers if not provider_allowed(config, name))
    parts = [f"no model is allowed for this {repo.privacy_class} repo"]
    if blocked:
        uses = ", ".join(f"{n} ({config.providers[n].data_use})" for n in blocked)
        parts.append(f"providers blocked by SAF-07 or provider_allow: {uses}")
        if repo.privacy_class != "public":
            parts.append(
                "to send this repo's code to them deliberately, set"
                " `allow_training_providers = true` under [repo] in <repo>/.arpeggio/config.toml"
                + (
                    ""
                    if repo.privacy_class == "client"
                    else " (or [privacy] allow_training_providers in the global config)"
                )
                + ', or mark a provider data_use = "no_training" after reading its terms'
            )
    if placeholders:
        parts.append(f"placeholder model ids: {', '.join(sorted(placeholders))}")
    return "; ".join(parts)


def require_allowed(config: Config) -> dict[str, ModelSpec]:
    models = allowed_models(config)
    if not models:
        raise NoAllowedRoute(why_none_allowed(config))
    return models


def present_tiers(models: Mapping[str, ModelSpec]) -> list[int]:
    return sorted({tier_of(key) for key in models})


def nearest_tier(models: Mapping[str, ModelSpec], tier: int) -> int:
    """``tier`` if present, else the nearest present tier, the lower one on a tie."""
    tiers = present_tiers(models)
    if not tiers:
        raise NoAllowedRoute("no allowed model")
    return min(tiers, key=lambda t: (abs(t - tier), t))


def cheapest(models: Mapping[str, ModelSpec], tier: int) -> str:
    """Cheapest model of ``tier``: lowest output price, then input price, then key."""
    candidates = [key for key in models if tier_of(key) == tier]
    if not candidates:
        raise NoAllowedRoute(f"no allowed model in tier{tier}")
    return min(
        candidates,
        key=lambda k: (models[k].price_out_per_m, models[k].price_in_per_m, k),
    )


def strongest(models: Mapping[str, ModelSpec]) -> str:
    """Highest tier present, then highest output price, then key name."""
    if not models:
        raise NoAllowedRoute("no allowed model")
    return min(models, key=lambda k: (-tier_of(k), -models[k].price_out_per_m, k))


def nearest_effort(spec: ModelSpec, requested: Effort) -> Effort:
    """``requested`` if the model allows it, else the nearest allowed level (lower on a tie)."""
    if requested in spec.efforts:
        return requested
    want = EFFORT_ORDER.index(requested)
    return min(
        spec.efforts,
        key=lambda e: (abs(EFFORT_ORDER.index(e) - want), EFFORT_ORDER.index(e)),
    )


def highest_effort(spec: ModelSpec) -> Effort:
    return max(spec.efforts, key=EFFORT_ORDER.index)


def route(models: Mapping[str, ModelSpec], key: str, effort: Effort) -> ResolvedRoute:
    return ResolvedRoute(key, nearest_effort(models[key], effort), effort)


def resolve_tier(
    models: Mapping[str, ModelSpec], tier: int | Strongest, effort: Effort
) -> ResolvedRoute:
    """``tier: N`` is the cheapest model of tier N or the nearest present tier;
    ``tier: strongest`` is ``strongest``."""
    key = strongest(models) if tier == "strongest" else cheapest(models, nearest_tier(models, tier))
    return route(models, key, effort)
