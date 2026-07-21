from __future__ import annotations

import tomllib
from pathlib import Path


class ConfigError(Exception):
    pass


def load(cwd: Path) -> dict:
    path = cwd / "rbs.toml"
    if not path.is_file():
        raise ConfigError(f"no rbs.toml found in {cwd}")
    with path.open("rb") as handle:
        return tomllib.load(handle)


def engine_name(config: dict) -> str:
    name = config.get("engine")
    if not name:
        raise ConfigError("rbs.toml is missing the top-level `engine` key")
    return name
