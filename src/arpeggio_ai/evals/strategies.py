"""The four baseline strategies (EVL-03): which routes a task gets, in order.

Each strategy turns an eval task into a fixed route sequence: the first route, then the
escalation routes tried after a verification or patch failure. At most
``MAX_ATTEMPTS`` routes. Two consecutive identical routes are merged, since retrying the
same route is not escalation.

- ``junior``: the strongest allowed model at the highest effort it accepts. No escalation.
- ``middle``: a fixed per-category map at effort ``high`` (or the nearest). ``docs`` and
  ``config`` go to the cheapest of tier1, everything else to the cheapest of tier2 (the
  nearest tier present in both cases), ``expected_risk: high`` to the strongest model.
  ``[evals.middle]`` overrides the model per category. **Claims compare against it.**
- ``senior``: the cheapest of the lowest tier present at ``low``; then the cheapest of the
  next tier present at ``medium``; then the strongest at ``high``.
- ``arpeggio``: the first matching rule of the policy file (``routing/policy.py``).

Risk is the task's ``expected_risk`` in M0.6; the signal-based classifier is v1.
"""

from dataclasses import dataclass, field
from typing import Any, Literal, get_args

from arpeggio_ai.config.models import Config, Effort
from arpeggio_ai.evals.task import EvalTask
from arpeggio_ai.routing.policy import PolicyFile
from arpeggio_ai.routing.resolve import (
    NoAllowedRoute,
    ResolvedRoute,
    cheapest,
    highest_effort,
    nearest_tier,
    present_tiers,
    require_allowed,
    resolve_tier,
    route,
    strongest,
)

Strategy = Literal["junior", "middle", "senior", "arpeggio"]
STRATEGIES: tuple[Strategy, ...] = get_args(Strategy)
REFERENCE: Strategy = "middle"
MAX_ATTEMPTS = 3
CHEAP_CATEGORIES = frozenset({"docs", "config"})


@dataclass(frozen=True, slots=True)
class PlannedRoute:
    model: str
    effort: Effort
    requested_effort: Effort
    reason: dict[str, Any] = field(default_factory=dict)  # stored in attempts.route_reason


def _planned(
    strategy: Strategy, routes: list[tuple[ResolvedRoute, dict[str, Any]]]
) -> list[PlannedRoute]:
    planned: list[PlannedRoute] = []
    for resolved, extra in routes:
        if planned and (planned[-1].model, planned[-1].effort) == (
            resolved.model,
            resolved.effort,
        ):
            continue
        reason = {
            "mode": "eval",
            "strategy": strategy,
            "step": len(planned),
            "requested_effort": resolved.requested_effort,
            **extra,
        }
        planned.append(
            PlannedRoute(resolved.model, resolved.effort, resolved.requested_effort, reason)
        )
    return planned[:MAX_ATTEMPTS]


def plan_routes(
    strategy: Strategy, task: EvalTask, config: Config, policy: PolicyFile
) -> list[PlannedRoute]:
    """The route sequence for ``task`` under ``config`` (merged with its repo's config).

    Raises ``NoAllowedRoute`` when no allowed model fits; never falls back to a model that
    the profile, SAF-07 or the placeholder guard excludes.
    """
    models = require_allowed(config)
    steps: list[tuple[ResolvedRoute, dict[str, Any]]] = []
    if strategy == "junior":
        top = strongest(models)
        steps.append((route(models, top, highest_effort(models[top])), {}))
    elif strategy == "middle":
        override = config.evals.middle.get(task.category)
        if override is not None:
            if override not in models:
                raise NoAllowedRoute(
                    f"[evals.middle] {task.category} = {override!r} is not allowed for this"
                    " repo (profile, SAF-07 or placeholder)"
                )
            steps.append((route(models, override, "high"), {"map": "config"}))
        elif task.expected_risk == "high":
            steps.append((route(models, strongest(models), "high"), {"map": "high-risk"}))
        else:
            tier = 1 if task.category in CHEAP_CATEGORIES else 2
            key = cheapest(models, nearest_tier(models, tier))
            steps.append((route(models, key, "high"), {"map": f"tier{tier}"}))
    elif strategy == "senior":
        tiers = present_tiers(models)
        steps.append((route(models, cheapest(models, tiers[0]), "low"), {}))
        if len(tiers) > 1:
            steps.append((route(models, cheapest(models, tiers[1]), "medium"), {}))
        steps.append((route(models, strongest(models), "high"), {}))
    else:
        rule = policy.match(task.category, task.expected_risk)
        if rule is None:
            raise NoAllowedRoute(
                f"no policy rule matches category {task.category}, risk {task.expected_risk}"
            )
        for ref in [rule.route, *rule.escalation]:
            steps.append((resolve_tier(models, ref.tier, ref.effort), {"rule": rule.id}))
    return _planned(strategy, steps)
