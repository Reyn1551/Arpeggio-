"""Build small configs for routing and strategy tests without a template file."""

from typing import Any

from arpeggio_ai.config.models import Config, parse_config

# key -> (provider, price_out_per_m, price_in_per_m, efforts)
ModelRow = tuple[str, float, float, list[str]]

DEFAULT_MODELS: dict[str, ModelRow] = {
    "tier1.flash": ("deep", 1.2, 0.3, ["low"]),
    "tier2.flash": ("deep", 1.2, 0.3, ["medium", "high"]),
    "tier3.pro": ("deep", 3.96, 1.32, ["high", "max"]),
}


def build_config(
    models: dict[str, ModelRow] | None = None,
    *,
    providers: dict[str, str] | None = None,
    profile: str = "micro",
    repo: dict[str, Any] | None = None,
    privacy: bool | None = None,
    evals: dict[str, Any] | None = None,
    placeholders: tuple[str, ...] = (),
    peak_utc: list[str] | None = None,
    **budget: Any,
) -> Config:
    """``providers`` maps name -> data_use. ``profile="free"`` makes every model free.

    ``repo`` defaults to a public repo, so SAF-07 allows everything unless a test says
    otherwise.
    """
    models = DEFAULT_MODELS if models is None else models
    providers = providers or {"deep": "unknown"}
    free = profile == "free"
    data: dict[str, Any] = {
        "budget": {
            "profile": profile,
            "per_task_usd": 0 if free else 0.2,
            "per_day_usd": 0 if free else 0.5,
            "per_month_usd": 0 if free else 2.0,
            **({} if free or profile != "micro" else {"prepaid_balance_usd": 2.0}),
            **budget,
        },
        "providers": {
            name: {
                "kind": "openai_compatible",
                "base_url": f"https://{name}.example.com",
                "api_key": f"env:{name.upper()}_KEY",
                "data_use": data_use,
                **(
                    {"pricing_windows": {"peak_utc": peak_utc, "offpeak_multiplier": 0.5}}
                    if peak_utc
                    else {}
                ),
            }
            for name, data_use in providers.items()
        },
        "models": {
            key: {
                "provider": provider,
                "model": f"<{key}>" if key in placeholders else f"m-{key}",
                "efforts": efforts,
                "price_in_per_m": 0.0 if free else price_in,
                "price_out_per_m": 0.0 if free else price_out,
                "free": free,
                "last_verified": "2026-10-01",
            }
            for key, (provider, price_out, price_in, efforts) in models.items()
        },
        "defaults": {
            "counterfactual_route": {
                "adapter": "api",
                "model": max(models, key=lambda k: (k.split(".")[0], models[k][1])),
                "effort": models[max(models, key=lambda k: (k.split(".")[0], models[k][1]))][3][-1],
            }
        },
    }
    data["repo"] = {"privacy_class": "public"} if repo is None else repo
    if privacy is not None:
        data["privacy"] = {"allow_training_providers": privacy}
    if evals is not None:
        data["evals"] = evals
    return parse_config(data)
