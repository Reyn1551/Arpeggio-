"""Adapter registry: name -> factory. Core code creates adapters only through ``create``.

Built-in adapters are imported lazily inside their factory, so a CLI command that makes no
model call never loads httpx.
"""

from collections.abc import Callable

from arpeggio_ai.adapters.base import Adapter, AdapterContext
from arpeggio_ai.core.errors import AdapterError

AdapterFactory = Callable[[AdapterContext], Adapter]

_factories: dict[str, AdapterFactory] = {}


def register(name: str, factory: AdapterFactory) -> None:
    if name in _factories:
        raise AdapterError(f"adapter {name!r} is already registered")
    _factories[name] = factory


def create(name: str, context: AdapterContext) -> Adapter:
    factory = _factories.get(name)
    if factory is None:
        known = ", ".join(names()) or "none"
        raise AdapterError(f"unknown adapter {name!r} (registered: {known})")
    return factory(context)


def names() -> list[str]:
    return sorted(_factories)


def _api(context: AdapterContext) -> Adapter:
    from arpeggio_ai.adapters.api import ApiAdapter  # imported here so httpx loads lazily

    return ApiAdapter(context)


register("api", _api)
