from __future__ import annotations

from redux_build.engines.base import Engine
from redux_build.engines.uv import UvEngine


class UnknownEngine(Exception):
    pass


ENGINES: dict[str, type[Engine]] = {
    UvEngine.name: UvEngine,
}


def get_engine(name: str, config: dict) -> Engine:
    try:
        engine_cls = ENGINES[name]
    except KeyError:
        raise UnknownEngine(
            f"unknown engine {name!r}; known engines: {sorted(ENGINES)}"
        ) from None
    return engine_cls(config)
