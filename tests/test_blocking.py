from redux_build.context import RunContext
from redux_build.engines.base import Engine
from redux_build.engines.dotnet import DotnetEngine
from redux_build.engines.npm import NpmEngine
from redux_build.engines.uv import UvEngine
from redux_build.models import Fragment, Status
from redux_build.report import find_blocker, write_fragment


def _ctx(tmp_path):
    return RunContext(
        cwd=tmp_path, is_github=False, env={"RBS_REPORT_DIR": str(tmp_path)}
    )


def _record(tmp_path, operation, status, variant=""):
    write_fragment(
        _ctx(tmp_path),
        Fragment(engine="npm", operation=operation, status=status, variant=variant),
    )


def test_no_blocker_when_nothing_required(tmp_path):
    assert find_blocker(tmp_path, []) is None


def test_no_blocker_when_prerequisite_succeeded(tmp_path):
    _record(tmp_path, "build", Status.success)
    assert find_blocker(tmp_path, ["build"]) is None


def test_failed_prerequisite_blocks(tmp_path):
    _record(tmp_path, "build", Status.success)
    _record(tmp_path, "integration-test", Status.failure)
    assert find_blocker(tmp_path, ["build", "integration-test"]) == "integration-test"


def test_blocked_prerequisite_cascades(tmp_path):
    # build failed -> integration-test blocked -> push must also block, not silently pass.
    _record(tmp_path, "build", Status.failure)
    _record(tmp_path, "integration-test", Status.blocked)
    assert find_blocker(tmp_path, ["integration-test"]) == "integration-test"


def test_missing_prerequisite_does_not_block(tmp_path):
    # A prerequisite that never ran is not a failure; the report still shows it absent.
    assert find_blocker(tmp_path, ["build"]) is None


def test_quality_gates_have_no_prerequisites():
    # audit/format-check/lint/typecheck/unit-test must always run so one pass reports everything.
    for operation in ("audit", "format-check", "lint", "typecheck", "unit-test"):
        assert Engine.requires.get(operation, []) == []


def test_artifact_chain_declares_prerequisites():
    assert Engine.requires["integration-test"] == ["build"]


def test_push_requires_every_operation_before_it():
    # An image in the registry has to mean every gate passed. Checked against each engine's own
    # `order` so adding an operation without gating push on it fails here.
    for engine_cls in (UvEngine, NpmEngine, DotnetEngine):
        expected = [op for op in engine_cls.order if op != "push"]
        assert engine_cls.requires["push"] == expected


def test_every_engine_inherits_the_chain():
    for engine_cls in (UvEngine, NpmEngine, DotnetEngine):
        assert engine_cls.requires["integration-test"] == ["build"]


def test_failed_quality_gate_blocks_push(tmp_path):
    _record(tmp_path, "lint", Status.failure)
    _record(tmp_path, "build", Status.success)
    _record(tmp_path, "integration-test", Status.success)
    assert find_blocker(tmp_path, Engine.requires["push"]) == "lint"


def test_skipped_gate_does_not_block_push(tmp_path):
    # `typecheck` has no gate on uv, and a repo may have no integration suite: not applicable
    # is not the same as failed.
    _record(tmp_path, "typecheck", Status.skipped)
    _record(tmp_path, "integration-test", Status.skipped)
    _record(tmp_path, "build", Status.success)
    assert find_blocker(tmp_path, Engine.requires["push"]) is None


def test_any_failing_variant_blocks(tmp_path):
    # Fragments are read in filename order, so 3.13 used to overwrite 3.12 and hide its failure.
    _record(tmp_path, "unit-test", Status.failure, variant="3.12")
    _record(tmp_path, "unit-test", Status.success, variant="3.13")
    assert find_blocker(tmp_path, ["unit-test"]) == "unit-test"


def test_blocked_is_not_ok():
    fragment = Fragment(engine="npm", operation="push", status=Status.blocked)
    assert not fragment.ok
