import json
from pathlib import Path

from redux_build.context import RunContext
from redux_build.engines import base as basemod
from redux_build.engines.dotnet import DotnetEngine, uses_mtp
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


def test_diagnostic_after_a_build_line_keeps_its_location(tmp_path, monkeypatch):
    # #8: each copy follows a different build line, which the file name used to swallow.
    warning = "/src/Gen.cs(37,22): warning CS3021: not needed [/src/API.csproj]\n"
    out = (
        "  Determining projects to restore...\n"
        + warning
        + "  API -> /src/bin/Release/net6.0/API.dll\n"
        + warning
    )
    monkeypatch.setattr(basemod, "run", _fixed(0, out))
    frag = DotnetEngine({}).lint(_ctx(tmp_path))
    assert [finding.location for finding in frag.findings] == ["/src/Gen.cs:37"]


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


# ── coverage gate ────────────────────────────────────────────────────────────


def _cobertura(covered, valid):
    return (
        f'<?xml version="1.0"?><coverage line-rate="{covered / valid:.4f}" '
        f'lines-covered="{covered}" lines-valid="{valid}"></coverage>'
    )


def _with_coverage(monkeypatch, reports):
    """Stand in for the collector: drop cobertura files into --results-directory."""

    def _run(cmd, *_args, **_kwargs):
        results = Path(cmd[cmd.index("--results-directory") + 1])
        for index, body in enumerate(reports):
            target = results / f"run{index}" / "coverage.cobertura.xml"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body)
        return CmdResult(rc=0, out=TEST_OUT, duration_s=0.1)

    monkeypatch.setattr(basemod, "run", _run)


def test_coverage_appended_to_the_summary(tmp_path, monkeypatch):
    _with_coverage(monkeypatch, [_cobertura(8293, 13500)])
    frag = DotnetEngine({}).unit_test(_ctx(tmp_path))
    assert frag.summary == "680 passed · coverage 61%"
    assert frag.status == Status.success


def test_coverage_below_minimum_fails_even_when_tests_pass(tmp_path, monkeypatch):
    _with_coverage(monkeypatch, [_cobertura(8293, 13500)])
    frag = DotnetEngine({"unit-test": {"coverage-min": 85}}).unit_test(_ctx(tmp_path))
    assert frag.status == Status.failure
    assert frag.summary == "680 passed · coverage 61% (min 85%)"


def test_coverage_at_the_minimum_passes(tmp_path, monkeypatch):
    _with_coverage(monkeypatch, [_cobertura(60, 100)])
    frag = DotnetEngine({"unit-test": {"coverage-min": 60}}).unit_test(_ctx(tmp_path))
    assert frag.status == Status.success


def test_reports_from_several_projects_are_summed(tmp_path, monkeypatch):
    # Summed, not averaged — a tiny fully-covered project must not mask a large bare one.
    _with_coverage(monkeypatch, [_cobertura(10, 10), _cobertura(0, 90)])
    frag = DotnetEngine({}).unit_test(_ctx(tmp_path))
    assert frag.summary == "680 passed · coverage 10%"


def test_coverage_recorded_as_a_metric(tmp_path, monkeypatch):
    _with_coverage(monkeypatch, [_cobertura(8293, 13500)])
    frag = DotnetEngine({}).unit_test(_ctx(tmp_path))
    assert frag.metrics == {"coverage": 61.4}


def test_no_gate_when_the_collector_produced_nothing(tmp_path, monkeypatch):
    _with_coverage(monkeypatch, [])
    frag = DotnetEngine({"unit-test": {"coverage-min": 85}}).unit_test(_ctx(tmp_path))
    assert frag.status == Status.success
    assert frag.summary == "680 passed"
    assert frag.metrics == {}


def test_malformed_cobertura_is_ignored(tmp_path, monkeypatch):
    _with_coverage(monkeypatch, ["<not-xml"])
    frag = DotnetEngine({}).unit_test(_ctx(tmp_path))
    assert frag.summary == "680 passed"


def test_collector_requested_with_a_clean_results_directory(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(basemod, "run", _capture(seen, out=TEST_OUT))
    DotnetEngine({}).unit_test(_ctx(tmp_path))
    assert "XPlat Code Coverage" in seen[0]
    assert "--no-build" not in seen[0]  # instrumentation needs a real build


def _results_dir(cmd):
    return cmd[cmd.index("--results-directory") + 1]


def test_vstest_command_is_unchanged(tmp_path, monkeypatch):
    # The exact command rbs ran before Microsoft Testing Platform support, for every VSTest repo.
    (tmp_path / "Redux.slnx").write_text("")
    seen = []
    monkeypatch.setattr(basemod, "run", _capture(seen, out=TEST_OUT))
    DotnetEngine({}).unit_test(_ctx(tmp_path))
    assert seen[0] == [
        "dotnet",
        "test",
        "Redux.slnx",
        "-c",
        "Release",
        "--collect",
        "XPlat Code Coverage",
        "--results-directory",
        _results_dir(seen[0]),
    ]


def test_vstest_filter_is_passed_through(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(basemod, "run", _capture(seen, out=TEST_OUT))
    config = {"unit-test": {"filter": "Category!=Performance"}}
    DotnetEngine(config).unit_test(_ctx(tmp_path))
    assert seen[0][-2:] == ["--filter", "Category!=Performance"]


# ── Microsoft Testing Platform ───────────────────────────────────────────────

# Real `dotnet test --solution … --no-ansi` output from SDK 10.0.103, xunit.v3 4.0.3 and
# Microsoft.Testing.Extensions.CodeCoverage 18.12.0 (paths shortened to /src).

MTP_PASSED = """\
Running tests from /src/mtp/Lib.Tests/bin/Release/net10.0/Lib.Tests.dll (net10.0|x64)
/src/mtp/Lib.Tests/bin/Release/net10.0/Lib.Tests.dll (net10.0|x64) passed (2s 549ms)

  In process file artifacts produced:
    - /src/res1/41749663-7ce4-46bb-b856-5904913a2331.cobertura.xml

Test run summary: Passed!
  total: 2
  failed: 0
  succeeded: 2
  skipped: 0
  duration: 5s 977ms
"""

MTP_FAILED = """\
Running tests from /src/mtp/Lib.Tests/bin/Release/net10.0/Lib.Tests.dll (net10.0|x64)
failed Lib.Tests.CalcTests.PerformanceIsExcluded (32ms)
  from /src/mtp/Lib.Tests/bin/Release/net10.0/Lib.Tests.dll (net10.0|x64)
  the Category!=Performance filter should have excluded this test
    at Lib.Tests.CalcTests.PerformanceIsExcluded() in /src/mtp/Lib.Tests/CalcTests.cs:16
/src/mtp/Lib.Tests/bin/Release/net10.0/Lib.Tests.dll (net10.0|x64) failed with 1 error(s) (1s 179ms)
Exit code: 2

Test run summary: Failed!
  total: 3
  failed: 1
  succeeded: 2
  skipped: 0
  duration: 1s 751ms
Test run completed with non-success exit code: 2 (see: https://aka.ms/testingplatform/exitcodes)
"""

MTP_ZERO = """\
Running tests from /src/mtp/Lib.Tests/bin/Release/net10.0/Lib.Tests.dll (net10.0|x64)
/src/mtp/Lib.Tests/bin/Release/net10.0/Lib.Tests.dll (net10.0|x64) Zero tests ran (972ms)
Exit code: 8

Test run summary: Zero tests ran
  error: 1

  total: 0
  failed: 0
  succeeded: 0
  skipped: 0
  duration: 1s 557ms
Test run completed with non-success exit code: 8 (see: https://aka.ms/testingplatform/exitcodes)
"""

MTP_NO_COVERAGE_PACKAGE = """\
/src/mtp/Lib.Tests/bin/Release/net10.0/Lib.Tests.dll (net10.0) Zero tests ran
Exit code: 5
  Standard output: Unknown option '--coverage'
  Option '--coverage' is provided by the 'Microsoft.Testing.Extensions.CodeCoverage' extension. Add a package reference to use it.
  Unknown option '--coverage-output-format'
  Option '--coverage-output-format' is provided by the 'Microsoft.Testing.Extensions.CodeCoverage' extension. Add a package reference to use it.

Test run summary: Zero tests ran
  error: 1

  total: 0
  failed: 0
  succeeded: 0
  skipped: 0
  duration: 510ms
Test run completed with non-success exit code: 1 (see: https://aka.ms/testingplatform/exitcodes)
"""

MTP_GLOBAL_JSON = '{\n  "test": {\n    "runner": "Microsoft.Testing.Platform"\n  }\n}\n'


def _mtp_repo(tmp_path, solution="Sample.slnx"):
    (tmp_path / "global.json").write_text(MTP_GLOBAL_JSON)
    if solution:
        (tmp_path / solution).write_text("")
    return tmp_path


def test_global_json_selects_mtp(tmp_path):
    assert uses_mtp(_mtp_repo(tmp_path))


def test_no_global_json_means_vstest(tmp_path):
    assert not uses_mtp(tmp_path)


def test_global_json_without_a_test_runner_means_vstest(tmp_path):
    (tmp_path / "global.json").write_text('{"sdk": {"version": "10.0.103"}}')
    assert not uses_mtp(tmp_path)


def test_global_json_vstest_runner_means_vstest(tmp_path):
    (tmp_path / "global.json").write_text('{"test": {"runner": "VSTest"}}')
    assert not uses_mtp(tmp_path)


def test_global_json_runner_value_is_case_insensitive(tmp_path):
    (tmp_path / "global.json").write_text(
        '{"test": {"runner": "microsoft.testing.platform"}}'
    )
    assert uses_mtp(tmp_path)


def test_global_json_keys_are_case_sensitive(tmp_path):
    # The SDK reads `test` / `runner` exactly; "Test" is just an unknown key to it.
    (tmp_path / "global.json").write_text(
        '{"Test": {"Runner": "Microsoft.Testing.Platform"}}'
    )
    assert not uses_mtp(tmp_path)


def test_global_json_allows_comments_and_trailing_commas(tmp_path):
    (tmp_path / "global.json").write_text(
        "{\n"
        "  // the SDK allows comments here\n"
        '  "sdk": { "version": "10.0.103", /* pinned */ },\n'
        '  "test": {\n'
        '    "runner": "Microsoft.Testing.Platform", // MTP\n'
        "  },\n"
        "}\n"
    )
    assert uses_mtp(tmp_path)


def test_comment_markers_inside_strings_are_kept(tmp_path):
    (tmp_path / "global.json").write_text(
        '{"msbuild-sdks": {"x": "http://a/*b*/"}, '
        '"test": {"runner": "Microsoft.Testing.Platform"}}'
    )
    assert uses_mtp(tmp_path)


def test_global_json_found_in_a_parent_folder(tmp_path):
    _mtp_repo(tmp_path, solution=None)
    nested = tmp_path / "repo"
    nested.mkdir()
    assert uses_mtp(nested)


def test_nearest_global_json_wins(tmp_path):
    # The SDK stops at the first global.json it finds, even one with no `test` section.
    _mtp_repo(tmp_path, solution=None)
    nested = tmp_path / "repo"
    nested.mkdir()
    (nested / "global.json").write_text('{"sdk": {"version": "10.0.103"}}')
    assert not uses_mtp(nested)


def test_malformed_global_json_means_vstest(tmp_path):
    (tmp_path / "global.json").write_text("{ not json")
    assert not uses_mtp(tmp_path)


def test_mtp_command(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(basemod, "run", _capture(seen, out=MTP_PASSED))
    DotnetEngine({}).unit_test(_ctx(_mtp_repo(tmp_path)))
    assert seen == [
        [
            "dotnet",
            "test",
            "--solution",
            "Sample.slnx",
            "-c",
            "Release",
            "--no-ansi",
            "--no-progress",
            "--results-directory",
            _results_dir(seen[0]),
            "--coverage",
            "--coverage-output-format",
            "cobertura",
        ]
    ]


def test_mtp_names_a_project_with_project(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(basemod, "run", _capture(seen, out=MTP_PASSED))
    config = {"solution": "tests/Lib.Tests.csproj"}
    DotnetEngine(config).unit_test(_ctx(_mtp_repo(tmp_path, solution=None)))
    assert seen[0][2:4] == ["--project", "tests/Lib.Tests.csproj"]


def test_mtp_without_a_solution_names_nothing(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(basemod, "run", _capture(seen, out=MTP_PASSED))
    DotnetEngine({}).unit_test(_ctx(_mtp_repo(tmp_path, solution=None)))
    assert seen[0][2:4] == ["-c", "Release"]


def test_mtp_filter_is_passed_through(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(basemod, "run", _capture(seen, out=MTP_PASSED))
    config = {"unit-test": {"filter": "Category!=Performance"}}
    DotnetEngine(config).unit_test(_ctx(_mtp_repo(tmp_path)))
    assert seen[0][-2:] == ["--filter", "Category!=Performance"]


def test_runner_mtp_forces_mtp_without_global_json(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(basemod, "run", _capture(seen, out=MTP_PASSED))
    DotnetEngine({"unit-test": {"runner": "mtp"}}).unit_test(_ctx(tmp_path))
    assert "--coverage" in seen[0]


def test_runner_vstest_overrides_global_json(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(basemod, "run", _capture(seen, out=TEST_OUT))
    config = {"unit-test": {"runner": "vstest"}}
    frag = DotnetEngine(config).unit_test(_ctx(_mtp_repo(tmp_path)))
    assert seen[0][2] == "Sample.slnx"
    assert "XPlat Code Coverage" in seen[0]
    assert frag.summary == "680 passed"


def test_unknown_runner_fails_without_running(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(basemod, "run", _capture(seen))
    frag = DotnetEngine({"unit-test": {"runner": "nunit"}}).unit_test(_ctx(tmp_path))
    assert seen == []
    assert frag.status == Status.failure
    assert (
        frag.summary
        == "unknown `[unit-test] runner` 'nunit'; use `auto`, `vstest`, `mtp`"
    )


def test_mtp_reports_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(basemod, "run", _fixed(0, MTP_PASSED))
    frag = DotnetEngine({}).unit_test(_ctx(_mtp_repo(tmp_path)))
    assert frag.status == Status.success
    assert frag.summary == "2 passed"


def test_mtp_reports_failures_first(tmp_path, monkeypatch):
    monkeypatch.setattr(basemod, "run", _fixed(2, MTP_FAILED))
    frag = DotnetEngine({}).unit_test(_ctx(_mtp_repo(tmp_path)))
    assert frag.status == Status.failure
    assert frag.summary == "1 failed · 2 passed"


def test_mtp_counts_skipped(tmp_path, monkeypatch):
    out = MTP_PASSED.replace("skipped: 0", "skipped: 4")
    monkeypatch.setattr(basemod, "run", _fixed(0, out))
    frag = DotnetEngine({}).unit_test(_ctx(_mtp_repo(tmp_path)))
    assert frag.summary == "2 passed · 4 skipped"


def test_mtp_zero_tests_ran_fails(tmp_path, monkeypatch):
    # MTP exits 8 when nothing ran (a filter that matched nothing, say); VSTest exits 0.
    monkeypatch.setattr(basemod, "run", _fixed(8, MTP_ZERO))
    frag = DotnetEngine({}).unit_test(_ctx(_mtp_repo(tmp_path)))
    assert frag.status == Status.failure
    assert frag.summary == "no tests ran"


def test_mtp_with_no_recognisable_output(tmp_path, monkeypatch):
    monkeypatch.setattr(basemod, "run", _fixed(1, "crashed"))
    frag = DotnetEngine({}).unit_test(_ctx(_mtp_repo(tmp_path)))
    assert frag.summary == "no tests reported"


def _with_mtp_coverage(monkeypatch, reports, out=MTP_PASSED):
    """Stand in for the CodeCoverage extension: it writes <guid>.cobertura.xml files flat into
    --results-directory, not into a folder per run as coverlet does."""

    def _run(cmd, *_args, **_kwargs):
        results = Path(_results_dir(cmd))
        for index, body in enumerate(reports):
            (results / f"4174966{index}-7ce4.cobertura.xml").write_text(body)
        return CmdResult(rc=0, out=out, duration_s=0.1)

    monkeypatch.setattr(basemod, "run", _run)


def test_mtp_coverage_appended_to_the_summary(tmp_path, monkeypatch):
    _with_mtp_coverage(monkeypatch, [_cobertura(2, 6)])
    frag = DotnetEngine({}).unit_test(_ctx(_mtp_repo(tmp_path)))
    assert frag.summary == "2 passed · coverage 33%"
    assert frag.metrics == {"coverage": 33.3}


def test_mtp_coverage_from_several_projects_is_summed(tmp_path, monkeypatch):
    _with_mtp_coverage(monkeypatch, [_cobertura(10, 10), _cobertura(0, 90)])
    frag = DotnetEngine({}).unit_test(_ctx(_mtp_repo(tmp_path)))
    assert frag.summary == "2 passed · coverage 10%"


def test_mtp_coverage_below_minimum_fails(tmp_path, monkeypatch):
    _with_mtp_coverage(monkeypatch, [_cobertura(2, 6)])
    config = {"unit-test": {"coverage-min": 80}}
    frag = DotnetEngine(config).unit_test(_ctx(_mtp_repo(tmp_path)))
    assert frag.status == Status.failure
    assert frag.summary == "2 passed · coverage 33% (min 80%)"


def test_mtp_without_the_coverage_package_reruns_without_coverage(
    tmp_path, monkeypatch
):
    # Like a VSTest project without coverlet.collector: no coverage gate, and the row says why.
    seen = []
    outputs = iter(
        [CmdResult(1, MTP_NO_COVERAGE_PACKAGE, 0.5), CmdResult(0, MTP_PASSED, 1.0)]
    )

    def _run(cmd, *_args, **_kwargs):
        seen.append(cmd)
        return next(outputs)

    monkeypatch.setattr(basemod, "run", _run)
    config = {"unit-test": {"coverage-min": 80, "filter": "Category!=Performance"}}
    frag = DotnetEngine(config).unit_test(_ctx(_mtp_repo(tmp_path)))
    assert len(seen) == 2
    assert "--coverage" in seen[0]
    assert "--coverage" not in seen[1]
    assert seen[1][-2:] == ["--filter", "Category!=Performance"]
    assert frag.status == Status.success
    assert frag.summary == (
        "2 passed · no coverage: reference `Microsoft.Testing.Extensions.CodeCoverage`"
    )
    assert frag.duration_s == 1.5
    assert frag.metrics == {}
