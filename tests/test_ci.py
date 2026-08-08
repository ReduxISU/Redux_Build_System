import json

from typer.testing import CliRunner

from redux_build.cli import app
from redux_build.engines import base as basemod
from redux_build.models import Status
from redux_build.report import load_fragments
from redux_build.runner import CmdResult

runner = CliRunner()


def _repo(tmp_path, dockerfile=True):
    (tmp_path / "rbs.toml").write_text('engine = "uv"\npackage = "demo"\n')
    if dockerfile:
        (tmp_path / "Dockerfile").write_text("FROM alpine\n")
    return tmp_path


def _fixed(rc, out=""):
    def _run(*_args, **_kwargs):
        return CmdResult(rc=rc, out=out, duration_s=0.1)

    return _run


def _invoke(tmp_path, monkeypatch, *args):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RBS_REPORT_DIR", str(tmp_path / ".rbs"))
    return runner.invoke(app, list(args))


def _statuses(tmp_path):
    return {f.operation: f.status for f in load_fragments(tmp_path / ".rbs")}


def test_ci_runs_every_operation_in_engine_order(tmp_path, monkeypatch):
    monkeypatch.setattr(basemod, "run", _fixed(0, "ok"))
    result = _invoke(_repo(tmp_path), monkeypatch, "ci", "--soft")
    assert result.exit_code == 0
    assert set(_statuses(tmp_path)) == {
        "audit",
        "format-check",
        "lint",
        "typecheck",
        "unit-test",
        "build",
        "integration-test",
        "push",
    }


def test_ci_renders_the_report_at_the_end(tmp_path, monkeypatch):
    monkeypatch.setattr(basemod, "run", _fixed(0, "ok"))
    result = _invoke(_repo(tmp_path), monkeypatch, "ci", "--soft")
    assert "Redux Build System — CI Report" in result.stdout
    assert (tmp_path / "report.md").is_file()


def test_ci_exits_nonzero_when_an_operation_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(basemod, "run", _fixed(1, "boom"))
    result = _invoke(_repo(tmp_path), monkeypatch, "ci")
    assert result.exit_code == 1


def test_ci_soft_exits_zero_despite_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(basemod, "run", _fixed(1, "boom"))
    result = _invoke(_repo(tmp_path), monkeypatch, "ci", "--soft")
    assert result.exit_code == 0


def test_quality_gates_all_run_even_after_a_failure(tmp_path, monkeypatch):
    # The whole point: one pass reports every problem, not just the first.
    monkeypatch.setattr(basemod, "run", _fixed(1, "boom"))
    _invoke(_repo(tmp_path), monkeypatch, "ci", "--soft")
    statuses = _statuses(tmp_path)
    for gate in ("audit", "format-check", "lint", "unit-test"):
        assert statuses[gate] == Status.failure, f"{gate} should have run"


def test_artifact_chain_blocks_when_build_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(basemod, "run", _fixed(1, "boom"))
    _invoke(_repo(tmp_path), monkeypatch, "ci", "--soft")
    statuses = _statuses(tmp_path)
    assert statuses["build"] == Status.failure
    assert statuses["integration-test"] == Status.blocked
    assert statuses["push"] == Status.blocked


def test_ci_clears_fragments_from_a_previous_run(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    stale = repo / ".rbs"
    stale.mkdir()
    (stale / "lint.json").write_text(
        json.dumps({"engine": "uv", "operation": "lint", "status": "failure"})
    )
    monkeypatch.setattr(basemod, "run", _fixed(0, "ok"))
    _invoke(repo, monkeypatch, "ci", "--soft")
    assert _statuses(tmp_path)["lint"] == Status.success


def test_ci_reports_config_error_as_exit_2(tmp_path, monkeypatch):
    result = _invoke(tmp_path, monkeypatch, "ci")  # no rbs.toml
    assert result.exit_code == 2
    assert "no rbs.toml" in result.stderr


def test_variant_is_carried_onto_every_fragment(tmp_path, monkeypatch):
    monkeypatch.setattr(basemod, "run", _fixed(0, "ok"))
    _invoke(_repo(tmp_path), monkeypatch, "ci", "--soft", "--variant", "py3.12")
    fragments = load_fragments(tmp_path / ".rbs")
    assert fragments
    assert all(f.variant == "py3.12" for f in fragments)
