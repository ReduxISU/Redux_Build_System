import json

from redux_build.context import RunContext
from redux_build.engines import base as basemod
from redux_build.engines.npm import NpmEngine
from redux_build.models import Status
from redux_build.runner import CmdResult


def _ctx(tmp_path):
    return RunContext(cwd=tmp_path, is_github=False, env={})


def _fixed(rc, out):
    def _run(*_args, **_kwargs):
        return CmdResult(rc=rc, out=out, duration_s=0.1)

    return _run


def _package_json(tmp_path, scripts):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": scripts}))


def test_audit_success(tmp_path, monkeypatch):
    monkeypatch.setattr(basemod, "run", _fixed(0, "found 0 vulnerabilities"))
    frag = NpmEngine({}).audit(_ctx(tmp_path))
    assert frag.status == Status.success
    assert frag.summary == "no known vulnerabilities"


def test_audit_failure_parses_count(tmp_path, monkeypatch):
    monkeypatch.setattr(
        basemod, "run", _fixed(1, "\n4 high severity vulnerabilities\n")
    )
    frag = NpmEngine({}).audit(_ctx(tmp_path))
    assert frag.status == Status.failure
    assert frag.summary == "4 high severity vulnerabilities"


def test_audit_omits_dev_dependencies(tmp_path, monkeypatch):
    seen = []

    def _capture(cmd, *_args, **_kwargs):
        seen.append(cmd)
        return CmdResult(rc=0, out="", duration_s=0.1)

    monkeypatch.setattr(basemod, "run", _capture)
    NpmEngine({}).audit(_ctx(tmp_path))
    assert seen == [["npm", "audit", "--omit=dev"]]


def test_format_check_failure_parses_count(tmp_path, monkeypatch):
    out = "Checked 65 files in 13ms. No fixes applied.\nFound 84 errors.\n"
    monkeypatch.setattr(basemod, "run", _fixed(1, out))
    frag = NpmEngine({}).format_check(_ctx(tmp_path))
    assert frag.status == Status.failure
    assert frag.summary == "Found 84 errors"


def test_lint_failure_parses_error_and_warning_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(
        basemod, "run", _fixed(1, "✖ 69 problems (26 errors, 43 warnings)\n")
    )
    frag = NpmEngine({}).lint(_ctx(tmp_path))
    assert frag.status == Status.failure
    assert frag.summary == "26 errors, 43 warnings"


def test_lint_reports_warnings_even_when_passing(tmp_path, monkeypatch):
    # eslint exits 0 when only warnings remain; the summary must still show them.
    monkeypatch.setattr(
        basemod, "run", _fixed(0, "✖ 43 problems (0 errors, 43 warnings)\n")
    )
    frag = NpmEngine({}).lint(_ctx(tmp_path))
    assert frag.status == Status.success
    assert frag.summary == "0 errors, 43 warnings"


def test_lint_clean(tmp_path, monkeypatch):
    monkeypatch.setattr(basemod, "run", _fixed(0, ""))
    frag = NpmEngine({}).lint(_ctx(tmp_path))
    assert frag.status == Status.success
    assert frag.summary == "0 problems"


def test_unit_test_skips_without_test_script(tmp_path):
    _package_json(tmp_path, {"build": "next build"})
    frag = NpmEngine({}).unit_test(_ctx(tmp_path))
    assert frag.status == Status.skipped
    assert "no `test` script" in frag.summary


def test_unit_test_skips_without_package_json(tmp_path):
    frag = NpmEngine({}).unit_test(_ctx(tmp_path))
    assert frag.status == Status.skipped


def test_unit_test_runs_when_script_exists(tmp_path, monkeypatch):
    _package_json(tmp_path, {"test": "vitest run"})
    monkeypatch.setattr(basemod, "run", _fixed(0, "Tests  12 passed (12)"))
    frag = NpmEngine({}).unit_test(_ctx(tmp_path))
    assert frag.status == Status.success
    assert "12 passed" in frag.summary


def test_unit_test_failure_reports_failed_count(tmp_path, monkeypatch):
    _package_json(tmp_path, {"test": "vitest run"})
    monkeypatch.setattr(basemod, "run", _fixed(1, "Tests  2 failed | 10 passed (12)"))
    frag = NpmEngine({}).unit_test(_ctx(tmp_path))
    assert frag.status == Status.failure
    assert "2 failed" in frag.summary
