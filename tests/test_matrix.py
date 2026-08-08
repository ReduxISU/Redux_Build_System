from typer.testing import CliRunner

from redux_build.cli import app
from redux_build.context import RunContext
from redux_build.engines import base as basemod
from redux_build.engines.base import Engine
from redux_build.engines.npm import NpmEngine
from redux_build.engines.uv import UvEngine, _python_version
from redux_build.report import load_fragments
from redux_build.runner import CmdResult

runner = CliRunner()

MATRIX = {"package": "demo", "unit-test": {"python-versions": ["3.12", "3.13"]}}


def _ctx(tmp_path, variant=""):
    return RunContext(cwd=tmp_path, is_github=False, variant=variant, env={})


def _capture(seen):
    def _run(cmd, *_args, **_kwargs):
        seen.append(cmd)
        return CmdResult(rc=0, out="1 passed", duration_s=0.1)

    return _run


# ── variant → interpreter ────────────────────────────────────────────────────


def test_version_parsed_from_either_label_form():
    assert _python_version("3.12") == "3.12"
    assert _python_version("py3.12") == "3.12"


def test_non_version_labels_select_no_interpreter():
    # A plain matrix tag must not be mistaken for a version.
    for label in ("", "linux", "fast", "py3", "3.12-slim"):
        assert _python_version(label) == ""


def test_unit_test_pins_the_interpreter_for_a_version_variant(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(basemod, "run", _capture(seen))
    UvEngine(MATRIX).unit_test(_ctx(tmp_path, "3.12"))
    assert seen[0][:4] == ["uv", "run", "--python", "3.12"]


def test_unit_test_uses_the_default_interpreter_without_a_version(
    tmp_path, monkeypatch
):
    seen = []
    monkeypatch.setattr(basemod, "run", _capture(seen))
    UvEngine(MATRIX).unit_test(_ctx(tmp_path))
    assert "--python" not in seen[0]
    assert seen[0][:3] == ["uv", "run", "pytest"]


def test_coverage_gate_still_applied_per_leg(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(basemod, "run", _capture(seen))
    config = {"package": "demo", "unit-test": {"coverage-min": 85}}
    UvEngine(config).unit_test(_ctx(tmp_path, "3.12"))
    assert "--cov=demo" in seen[0]
    assert "--cov-fail-under=85" in seen[0]


# ── which operations fan out ─────────────────────────────────────────────────


def test_only_unit_test_fans_out():
    engine = UvEngine(MATRIX)
    assert engine.variants("unit-test") == ["3.12", "3.13"]
    for operation in ("audit", "format-check", "lint", "typecheck", "build", "push"):
        assert engine.variants(operation) == []


def test_no_fan_out_without_declared_versions():
    assert UvEngine({"package": "demo"}).variants("unit-test") == []


def test_engines_without_a_matrix_return_nothing():
    assert Engine({}).variants("unit-test") == []
    assert NpmEngine({}).variants("unit-test") == []


def test_numeric_versions_in_toml_are_stringified():
    # TOML 3.12 without quotes parses as a float; the command needs a string.
    engine = UvEngine({"unit-test": {"python-versions": [3.12]}})
    assert engine.variants("unit-test") == ["3.12"]


# ── end to end through `rbs ci` ──────────────────────────────────────────────


def _repo(tmp_path):
    (tmp_path / "rbs.toml").write_text(
        'engine = "uv"\npackage = "demo"\n\n[unit-test]\npython-versions = ["3.12", "3.13"]\n'
    )
    return tmp_path


def test_ci_runs_unit_test_once_per_version(tmp_path, monkeypatch):
    monkeypatch.chdir(_repo(tmp_path))
    monkeypatch.setenv("RBS_REPORT_DIR", str(tmp_path / ".rbs"))
    monkeypatch.setattr(basemod, "run", _capture([]))
    runner.invoke(app, ["ci", "--soft"])
    legs = sorted(
        f.variant
        for f in load_fragments(tmp_path / ".rbs")
        if f.operation == "unit-test"
    )
    assert legs == ["3.12", "3.13"]


def test_each_leg_writes_its_own_fragment_file(tmp_path, monkeypatch):
    monkeypatch.chdir(_repo(tmp_path))
    monkeypatch.setenv("RBS_REPORT_DIR", str(tmp_path / ".rbs"))
    monkeypatch.setattr(basemod, "run", _capture([]))
    runner.invoke(app, ["ci", "--soft"])
    names = {path.name for path in (tmp_path / ".rbs").glob("unit-test*")}
    assert names == {"unit-test-3.12.json", "unit-test-3.13.json"}


def test_other_operations_still_run_once(tmp_path, monkeypatch):
    monkeypatch.chdir(_repo(tmp_path))
    monkeypatch.setenv("RBS_REPORT_DIR", str(tmp_path / ".rbs"))
    monkeypatch.setattr(basemod, "run", _capture([]))
    runner.invoke(app, ["ci", "--soft"])
    lint = [f for f in load_fragments(tmp_path / ".rbs") if f.operation == "lint"]
    assert len(lint) == 1


def test_explicit_variant_pins_to_one_leg(tmp_path, monkeypatch):
    monkeypatch.chdir(_repo(tmp_path))
    monkeypatch.setenv("RBS_REPORT_DIR", str(tmp_path / ".rbs"))
    monkeypatch.setattr(basemod, "run", _capture([]))
    runner.invoke(app, ["ci", "--soft", "--variant", "3.13"])
    legs = [
        f.variant
        for f in load_fragments(tmp_path / ".rbs")
        if f.operation == "unit-test"
    ]
    assert legs == ["3.13"]
