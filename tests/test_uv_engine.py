from redux_build.context import RunContext
from redux_build.engines import uv as uvmod
from redux_build.models import Status
from redux_build.runner import CmdResult


def _ctx(tmp_path):
    return RunContext(cwd=tmp_path, is_github=False, variant="py3.12", env={})


def _fixed(rc, out):
    def _run(*_args, **_kwargs):
        return CmdResult(rc=rc, out=out, duration_s=0.1)

    return _run


def test_search_extracts_match_or_default():
    assert (
        uvmod._search(r"Found \d+ error\w*", "Found 3 errors.", "x") == "Found 3 errors"
    )
    assert uvmod._search(r"Found \d+ error\w*", "clean", "default") == "default"


def test_lint_success(tmp_path, monkeypatch):
    monkeypatch.setattr(uvmod, "run", _fixed(0, "All checks passed!"))
    frag = uvmod.UvEngine({}).lint(_ctx(tmp_path))
    assert frag.status == Status.success
    assert frag.summary == "0 issues"
    assert frag.variant == "py3.12"


def test_lint_failure_parses_count(tmp_path, monkeypatch):
    monkeypatch.setattr(uvmod, "run", _fixed(1, "Found 3 errors."))
    frag = uvmod.UvEngine({}).lint(_ctx(tmp_path))
    assert frag.status == Status.failure
    assert "3 error" in frag.summary


def test_format_check_failure_parses_count(tmp_path, monkeypatch):
    monkeypatch.setattr(uvmod, "run", _fixed(1, "2 files would be reformatted"))
    frag = uvmod.UvEngine({}).format_check(_ctx(tmp_path))
    assert frag.status == Status.failure
    assert frag.summary == "2 files would be reformatted"


def test_unit_test_success_parses_pass_and_coverage(tmp_path, monkeypatch):
    out = "test_x PASSED\n=== 142 passed in 3.4s ===\nTOTAL   100   9   91%\n"
    monkeypatch.setattr(uvmod, "run", _fixed(0, out))
    config = {"package": "pkg", "unit-test": {"coverage-min": 85}}
    frag = uvmod.UvEngine(config).unit_test(_ctx(tmp_path))
    assert frag.status == Status.success
    assert "142 passed" in frag.summary
    assert "coverage 91%" in frag.summary


def test_unit_test_failure_reports_failed_count(tmp_path, monkeypatch):
    monkeypatch.setattr(uvmod, "run", _fixed(1, "=== 3 failed, 139 passed in 4s ==="))
    frag = uvmod.UvEngine({"package": "pkg"}).unit_test(_ctx(tmp_path))
    assert frag.status == Status.failure
    assert "3 failed" in frag.summary
