from pathlib import Path

import pytest

from redux_build.context import RunContext
from redux_build.engines.base import Engine
from redux_build.registry import UnknownEngine, get_engine


def test_uv_engine_resolves():
    engine = get_engine("uv", {})
    assert engine.name == "uv"
    assert "lint" in engine.order


def test_unknown_engine_raises():
    with pytest.raises(UnknownEngine):
        get_engine("cargo", {})


@pytest.mark.parametrize(
    "operation",
    [
        "audit",
        "format-check",
        "lint",
        "typecheck",
        "unit-test",
        "build",
        "integration-test",
        "push",
    ],
)
def test_base_engine_operation_skips(operation):
    engine = Engine({})
    engine.name = "base"
    fragment = engine.run_operation(operation, RunContext.detect(cwd=Path.cwd()))
    assert fragment.status == "skipped"
    assert fragment.ok
