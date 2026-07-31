import json
from pathlib import Path

from redux_build.context import RunContext
from redux_build.engines import base as basemod
from redux_build.engines.dotnet import DotnetEngine
from redux_build.models import Status
from redux_build.runner import CmdResult

# Fixtures below are the real shapes captured from `mcr.microsoft.com/dotnet/sdk:10.0`.

FORMAT_OUT = (
    "/src/redux-tests/Problems/NPH_PUMPSCHEDULINGEM/PUMPSCHEDULINGEM_Tests.cs(46,25): "
    "error WHITESPACE: Fix whitespace formatting. Delete 4 characters. "
    "[/src/redux-tests/redux-tests.csproj]\n"
    "/src/redux-tests/Problems/NPH_PUMPSCHEDULINGEM/PUMPSCHEDULINGEM_Tests.cs(48,27): "
    "error WHITESPACE: Fix whitespace formatting. Delete 2 characters. "
    "[/src/redux-tests/redux-tests.csproj]\n"
    "/src/Program.cs(12,5): error WHITESPACE: Fix whitespace formatting. "
    "[/src/API.csproj]\n"
)

BUILD_CLEAN = (
    "  API -> /src/bin/Release/net10.0/API.dll\n\nBuild succeeded.\n"
    "    0 Warning(s)\n    0 Error(s)\n\nTime Elapsed 00:00:03.69\n"
)

TEST_OUT = (
    "Test run for /src/redux-tests/bin/Release/net10.0/redux-tests.dll\n"
    "Passed!  - Failed:     0, Passed:   680, Skipped:     0, Total:   680, "
    "Duration: 4 s - redux-tests.dll (net10.0)\n"
)

AUDIT_CLEAN = json.dumps({"version": 1, "projects": [{"path": "/src/API.csproj"}]})

AUDIT_VULNERABLE = json.dumps(
    {
        "version": 1,
        "projects": [
            {
                "path": "/src/API.csproj",
                "frameworks": [
                    {
                        "framework": "net10.0",
                        "topLevelPackages": [
                            {
                                "id": "Newtonsoft.Json",
                                "resolvedVersion": "11.0.1",
                                "vulnerabilities": [
                                    {
                                        "severity": "High",
                                        "advisoryurl": "https://github.com/advisories/GHSA-5crp",
                                    }
                                ],
                            }
                        ],
                        "transitivePackages": [
                            {
                                "id": "System.Text.Json",
                                "resolvedVersion": "6.0.0",
                                "vulnerabilities": [
                                    {
                                        "severity": "Moderate",
                                        "advisoryurl": "https://github.com/advisories/GHSA-xyz",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }
)


def _ctx(tmp_path):
    return RunContext(cwd=tmp_path, is_github=False, env={})


def _fixed(rc, out=""):
    def _run(*_args, **_kwargs):
        return CmdResult(rc=rc, out=out, duration_s=0.1)

    return _run


def _capture(seen, rc=0, out=""):
    def _run(cmd, *_args, **_kwargs):
        seen.append(cmd)
        return CmdResult(rc=rc, out=out, duration_s=0.1)

    return _run


# ── solution discovery ───────────────────────────────────────────────────────


def test_solution_is_named_explicitly_from_disk(tmp_path, monkeypatch):
    # A bare `dotnet build` errors MSB1011 when a .csproj sits beside the solution.
    (tmp_path / "Redux.slnx").write_text("<Solution />")
    seen = []
    monkeypatch.setattr(basemod, "run", _capture(seen, out=BUILD_CLEAN))
    DotnetEngine({}).lint(_ctx(tmp_path))
    assert seen[0][:3] == ["dotnet", "build", "Redux.slnx"]


def test_configured_solution_wins(tmp_path, monkeypatch):
    (tmp_path / "Other.slnx").write_text("<Solution />")
    seen = []
    monkeypatch.setattr(basemod, "run", _capture(seen, out=BUILD_CLEAN))
    DotnetEngine({"solution": "Redux.slnx"}).lint(_ctx(tmp_path))
    assert seen[0][2] == "Redux.slnx"


def test_slnx_preferred_over_sln(tmp_path, monkeypatch):
    (tmp_path / "Redux.sln").write_text("")
    (tmp_path / "Redux.slnx").write_text("<Solution />")
    seen = []
    monkeypatch.setattr(basemod, "run", _capture(seen, out=BUILD_CLEAN))
    DotnetEngine({}).lint(_ctx(tmp_path))
    assert seen[0][2] == "Redux.slnx"


def test_audit_puts_the_solution_before_the_package_verb(tmp_path, monkeypatch):
    (tmp_path / "Redux.slnx").write_text("<Solution />")
    seen = []
    monkeypatch.setattr(basemod, "run", _capture(seen, out=AUDIT_CLEAN))
    DotnetEngine({}).audit(_ctx(tmp_path))
    assert seen[0][:4] == ["dotnet", "list", "Redux.slnx", "package"]
    assert "--include-transitive" in seen[0]


# ── audit ────────────────────────────────────────────────────────────────────


def test_audit_clean(tmp_path, monkeypatch):
    monkeypatch.setattr(basemod, "run", _fixed(0, AUDIT_CLEAN))
    frag = DotnetEngine({}).audit(_ctx(tmp_path))
    assert frag.status == Status.success
    assert frag.summary == "no known vulnerabilities"


def test_audit_fails_despite_a_zero_exit_code(tmp_path, monkeypatch):
    # `dotnet list package --vulnerable` exits 0 even when it finds vulnerabilities.
    monkeypatch.setattr(basemod, "run", _fixed(0, AUDIT_VULNERABLE))
    frag = DotnetEngine({}).audit(_ctx(tmp_path))
    assert frag.status == Status.failure
    assert frag.summary == "1 high · 1 moderate"


def test_audit_covers_transitive_packages(tmp_path, monkeypatch):
    monkeypatch.setattr(basemod, "run", _fixed(0, AUDIT_VULNERABLE))
    frag = DotnetEngine({}).audit(_ctx(tmp_path))
    locations = {finding.location for finding in frag.findings}
    assert locations == {"Newtonsoft.Json@11.0.1", "System.Text.Json@6.0.0"}
    assert frag.findings[0].severity == "high"  # most severe first
    assert "GHSA-5crp" in frag.findings[0].rule


def test_audit_survives_a_malformed_document(tmp_path, monkeypatch):
    monkeypatch.setattr(basemod, "run", _fixed(0, "not json"))
    frag = DotnetEngine({}).audit(_ctx(tmp_path))
    assert frag.findings == []
    assert frag.status == Status.success


# ── format-check ─────────────────────────────────────────────────────────────


def test_format_check_clean(tmp_path, monkeypatch):
    monkeypatch.setattr(basemod, "run", _fixed(0, ""))
    frag = DotnetEngine({}).format_check(_ctx(tmp_path))
    assert frag.status == Status.success
    assert frag.summary == "all files formatted"


def test_format_check_counts_files_not_diagnostics(tmp_path, monkeypatch):
    # Three diagnostics across two files should read as two files.
    monkeypatch.setattr(basemod, "run", _fixed(2, FORMAT_OUT))
    frag = DotnetEngine({}).format_check(
        RunContext(cwd=Path("/src"), is_github=False, env={})
    )
    assert frag.status == Status.failure
    assert frag.summary == "2 files need formatting"
    assert len(frag.findings) == 3


def test_diagnostic_paths_are_repo_relative(tmp_path, monkeypatch):
    monkeypatch.setattr(basemod, "run", _fixed(2, FORMAT_OUT))
    frag = DotnetEngine({}).format_check(
        RunContext(cwd=Path("/src"), is_github=False, env={})
    )
    assert "Program.cs:12" in {finding.location for finding in frag.findings}


def test_project_suffix_stripped_from_message(tmp_path, monkeypatch):
    monkeypatch.setattr(basemod, "run", _fixed(2, FORMAT_OUT))
    frag = DotnetEngine({}).format_check(_ctx(tmp_path))
    assert all(".csproj]" not in finding.message for finding in frag.findings)


# ── lint ─────────────────────────────────────────────────────────────────────


def test_lint_clean(tmp_path, monkeypatch):
    monkeypatch.setattr(basemod, "run", _fixed(0, BUILD_CLEAN))
    frag = DotnetEngine({}).lint(_ctx(tmp_path))
    assert frag.status == Status.success
    assert frag.summary == "0 issues"


def test_lint_reports_compiler_diagnostics(tmp_path, monkeypatch):
    out = (
        "/src/Program.cs(12,5): error CS0103: The name 'foo' does not exist "
        "[/src/API.csproj]\n"
        "/src/Other.cs(3,1): warning CS0168: Variable declared but never used "
        "[/src/API.csproj]\n"
    )
    monkeypatch.setattr(basemod, "run", _fixed(1, out))
    frag = DotnetEngine({}).lint(_ctx(tmp_path))
    assert frag.status == Status.failure
    assert frag.summary == "1 error, 1 warning"
    assert frag.findings[0].rule == "CS0103"


def test_duplicate_diagnostics_deduped(tmp_path, monkeypatch):
    # MSBuild repeats a diagnostic once per referencing project.
    line = "/src/Program.cs(12,5): error CS0103: nope [/src/API.csproj]\n"
    monkeypatch.setattr(basemod, "run", _fixed(1, line + line))
    frag = DotnetEngine({}).lint(_ctx(tmp_path))
    assert len(frag.findings) == 1


def test_lint_failure_without_parsable_diagnostics(tmp_path, monkeypatch):
    monkeypatch.setattr(basemod, "run", _fixed(1, "MSB1011: ambiguous project"))
    frag = DotnetEngine({}).lint(_ctx(tmp_path))
    assert frag.status == Status.failure
    assert frag.summary == "build failed"


# ── unit-test ────────────────────────────────────────────────────────────────


def test_unit_test_reports_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(basemod, "run", _fixed(0, TEST_OUT))
    frag = DotnetEngine({}).unit_test(_ctx(tmp_path))
    assert frag.status == Status.success
    assert frag.summary == "680 passed"


def test_unit_test_reports_failures_first(tmp_path, monkeypatch):
    out = "Failed!  - Failed:     3, Passed:   677, Skipped:     2, Total:   682\n"
    monkeypatch.setattr(basemod, "run", _fixed(1, out))
    frag = DotnetEngine({}).unit_test(_ctx(tmp_path))
    assert frag.status == Status.failure
    assert frag.summary == "3 failed · 677 passed · 2 skipped"


def test_unit_test_with_no_recognisable_output(tmp_path, monkeypatch):
    monkeypatch.setattr(basemod, "run", _fixed(1, "crashed"))
    frag = DotnetEngine({}).unit_test(_ctx(tmp_path))
    assert frag.summary == "no tests reported"
