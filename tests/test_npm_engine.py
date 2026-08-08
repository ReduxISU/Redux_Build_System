import json
from pathlib import Path

from redux_build.context import RunContext
from redux_build.engines import base as basemod
from redux_build.engines.npm import NpmEngine
from redux_build.models import Status
from redux_build.runner import CmdResult

# Fixtures below mirror the real documents these tools emit — captured from Redux_GUI.

ESLINT_JSON = json.dumps(
    [
        {
            "filePath": "/repo/components/Card.js",
            "messages": [
                {
                    "ruleId": "@next/next/no-img-element",
                    "severity": 1,
                    "message": "Using `<img>` could result in slower LCP.",
                    "line": 14,
                },
                {
                    "ruleId": "no-undef",
                    "severity": 2,
                    "message": "'foo' is not defined.",
                    "line": 3,
                },
            ],
        }
    ]
)

BIOME_JSON = json.dumps(
    {
        "summary": {"errors": 3, "warnings": 0, "infos": 1},
        "diagnostics": [
            {
                "severity": "error",
                "message": "Formatter would have printed the following content:",
                "category": "format",
                "location": {"path": "Tools/Constants.js"},
            },
            {
                "severity": "error",
                "message": "The imports are not sorted.",
                "category": "assist/source/organizeImports",
                "location": {"path": "pages/index.js"},
            },
        ],
    }
)

AUDIT_JSON = json.dumps(
    {
        "vulnerabilities": {
            "brace-expansion": {
                "name": "brace-expansion",
                "severity": "high",
                "range": "<=5.0.7",
                "via": [
                    {
                        "title": "brace-expansion: DoS via exponential-time expansion",
                        "url": "https://github.com/advisories/GHSA-3jxr-9vmj-r5cp",
                        "severity": "high",
                    }
                ],
            },
            "postcss": {
                "name": "postcss",
                "severity": "moderate",
                "range": "<8.4.31",
                "via": [],
            },
        }
    }
)


# Real `tsc --noEmit --pretty false` output, captured from Redux_VR. One line per diagnostic —
# note the TS2339's elaboration is indented beneath it and is not a diagnostic of its own.
TSC_OUT = """\
packages/layout/test/formula.test.ts(24,30): error TS2339: Property 'clauses' does not exist on \
type 'AnyFrame'.
  Property 'clauses' does not exist on type 'ApiGraphFrame'.
packages/layout/test/formula.test.ts(37,29): error TS7006: Parameter 'c' implicitly has an 'any' \
type.
packages/puzzle/test/layout.test.ts(37,12): error TS2532: Object is possibly 'undefined'.
"""


def _ctx(tmp_path):
    return RunContext(cwd=tmp_path, is_github=False, env={})


def _fixed(rc, out):
    def _run(*_args, **_kwargs):
        return CmdResult(rc=rc, out=out, duration_s=0.1)

    return _run


def _package_json(tmp_path, scripts):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": scripts}))


def _tsconfig(tmp_path):
    (tmp_path / "tsconfig.json").write_text("{}")


# ── audit ────────────────────────────────────────────────────────────────────


def test_audit_clean(tmp_path, monkeypatch):
    monkeypatch.setattr(basemod, "run", _fixed(0, json.dumps({"vulnerabilities": {}})))
    frag = NpmEngine({}).audit(_ctx(tmp_path))
    assert frag.status == Status.success
    assert frag.summary == "no known vulnerabilities"
    assert frag.findings == []


def test_audit_summarises_by_severity(tmp_path, monkeypatch):
    monkeypatch.setattr(basemod, "run", _fixed(1, AUDIT_JSON))
    frag = NpmEngine({}).audit(_ctx(tmp_path))
    assert frag.status == Status.failure
    assert frag.summary == "1 high · 1 moderate"


def test_audit_findings_carry_advisory_details(tmp_path, monkeypatch):
    monkeypatch.setattr(basemod, "run", _fixed(1, AUDIT_JSON))
    frag = NpmEngine({}).audit(_ctx(tmp_path))
    top = frag.findings[0]
    assert top.severity == "high"  # most severe first, so truncation keeps what matters
    assert top.location == "brace-expansion@<=5.0.7"
    assert "GHSA-3jxr-9vmj-r5cp" in top.rule
    assert "DoS" in top.message


def test_audit_without_advisory_still_reports_the_package(tmp_path, monkeypatch):
    monkeypatch.setattr(basemod, "run", _fixed(1, AUDIT_JSON))
    frag = NpmEngine({}).audit(_ctx(tmp_path))
    postcss = next(f for f in frag.findings if f.location.startswith("postcss"))
    assert "postcss" in postcss.message


def test_audit_omits_dev_dependencies(tmp_path, monkeypatch):
    seen = []

    def _capture(cmd, *_args, **_kwargs):
        seen.append(cmd)
        return CmdResult(rc=0, out="{}", duration_s=0.1)

    monkeypatch.setattr(basemod, "run", _capture)
    NpmEngine({}).audit(_ctx(tmp_path))
    assert seen == [["npm", "audit", "--omit=dev", "--json"]]


# ── format-check ─────────────────────────────────────────────────────────────


def test_format_check_clean(tmp_path, monkeypatch):
    monkeypatch.setattr(basemod, "run", _fixed(0, json.dumps({"diagnostics": []})))
    frag = NpmEngine({}).format_check(_ctx(tmp_path))
    assert frag.status == Status.success
    assert frag.summary == "all files formatted"


def test_format_check_splits_formatting_from_import_order(tmp_path, monkeypatch):
    monkeypatch.setattr(basemod, "run", _fixed(1, BIOME_JSON))
    frag = NpmEngine({}).format_check(_ctx(tmp_path))
    assert frag.status == Status.failure
    assert "1 format" in frag.summary
    assert "1 import order" in frag.summary
    assert {f.location for f in frag.findings} == {
        "Tools/Constants.js",
        "pages/index.js",
    }


def test_format_diagnostics_drop_the_boilerplate_message(tmp_path, monkeypatch):
    # Biome repeats "Formatter would have printed…" plus the whole diff on every file.
    monkeypatch.setattr(basemod, "run", _fixed(1, BIOME_JSON))
    frag = NpmEngine({}).format_check(_ctx(tmp_path))
    formatting = next(f for f in frag.findings if f.rule == "format")
    assert formatting.message == "needs formatting"


# ── lint ─────────────────────────────────────────────────────────────────────


def test_lint_counts_come_from_parsed_findings(tmp_path, monkeypatch):
    monkeypatch.setattr(basemod, "run", _fixed(1, ESLINT_JSON))
    frag = NpmEngine({}).lint(_ctx(tmp_path))
    assert frag.status == Status.failure
    assert frag.summary == "1 error, 1 warning"


def test_lint_findings_are_error_first_with_location(tmp_path, monkeypatch):
    monkeypatch.setattr(basemod, "run", _fixed(1, ESLINT_JSON))
    frag = NpmEngine({}).lint(RunContext(cwd=Path("/repo"), is_github=False, env={}))
    assert frag.findings[0].severity == "error"
    assert frag.findings[0].rule == "no-undef"
    assert frag.findings[0].location == "components/Card.js:3"  # relative to cwd


def test_lint_clean(tmp_path, monkeypatch):
    monkeypatch.setattr(basemod, "run", _fixed(0, "[]"))
    frag = NpmEngine({}).lint(_ctx(tmp_path))
    assert frag.status == Status.success
    assert frag.summary == "0 problems"


def test_lint_keeps_stderr_out_of_the_json(tmp_path, monkeypatch):
    # Merged, an npx or eslint notice on stderr lands inside the document and every finding
    # disappears into `_parse` — while the summary still reads plausibly.
    seen = {}

    def _capture(cmd, cwd, **kwargs):
        seen.update(kwargs)
        return CmdResult(rc=0, out="[]", duration_s=0.1)

    monkeypatch.setattr(basemod, "run", _capture)
    NpmEngine({}).lint(_ctx(tmp_path))
    assert seen["merge_stderr"] is False


def test_lint_falls_back_to_text_when_json_is_unparseable(tmp_path, monkeypatch):
    # A crashed tool must not crash rbs; the exit code still gates.
    monkeypatch.setattr(
        basemod,
        "run",
        _fixed(1, "Oops, eslint exploded\n✖ 69 problems (26 errors, 43 warnings)"),
    )
    frag = NpmEngine({}).lint(_ctx(tmp_path))
    assert frag.status == Status.failure
    assert frag.summary == "26 errors, 43 warnings"
    assert frag.findings == []


def test_malformed_json_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(basemod, "run", _fixed(1, "not json at all"))
    engine = NpmEngine({})
    for operation in ("audit", "format-check", "lint"):
        frag = engine.run_operation(operation, _ctx(tmp_path))
        assert frag.status == Status.failure
        assert frag.findings == []


# ── typecheck ────────────────────────────────────────────────────────────────


def test_typecheck_skips_without_a_tsconfig(tmp_path):
    # Redux_GUI is plain JS: reporting `skipped` beats passing vacuously.
    frag = NpmEngine({}).typecheck(_ctx(tmp_path))
    assert frag.status == Status.skipped
    assert frag.summary == "no tsconfig.json"


def test_typecheck_clean(tmp_path, monkeypatch):
    _tsconfig(tmp_path)
    monkeypatch.setattr(basemod, "run", _fixed(0, ""))
    frag = NpmEngine({}).typecheck(_ctx(tmp_path))
    assert frag.status == Status.success
    assert frag.summary == "no type errors"
    assert frag.findings == []


def test_typecheck_counts_errors_and_files(tmp_path, monkeypatch):
    _tsconfig(tmp_path)
    monkeypatch.setattr(basemod, "run", _fixed(2, TSC_OUT))
    frag = NpmEngine({}).typecheck(_ctx(tmp_path))
    assert frag.status == Status.failure
    assert frag.summary == "3 type errors in 2 files"


def test_typecheck_elaborations_are_not_findings(tmp_path, monkeypatch):
    # tsc indents a diagnostic's explanation beneath it; counted, every union mismatch inflates.
    _tsconfig(tmp_path)
    monkeypatch.setattr(basemod, "run", _fixed(2, TSC_OUT))
    frag = NpmEngine({}).typecheck(_ctx(tmp_path))
    assert len(frag.findings) == 3


def test_typecheck_findings_carry_code_and_location(tmp_path, monkeypatch):
    _tsconfig(tmp_path)
    monkeypatch.setattr(basemod, "run", _fixed(2, TSC_OUT))
    frag = NpmEngine({}).typecheck(_ctx(tmp_path))
    top = frag.findings[0]
    assert top.severity == "error"
    assert top.rule == "TS2339"
    assert top.location == "packages/layout/test/formula.test.ts:24"
    assert "'clauses' does not exist" in top.message


def test_typecheck_config_error_is_still_a_failure(tmp_path, monkeypatch):
    # TS5058 and friends carry no file:line, so nothing parses — but the run did not pass.
    _tsconfig(tmp_path)
    monkeypatch.setattr(
        basemod,
        "run",
        _fixed(1, "error TS5058: The specified path does not exist: 'nope.json'."),
    )
    frag = NpmEngine({}).typecheck(_ctx(tmp_path))
    assert frag.status == Status.failure
    assert frag.summary == "typecheck failed"
    assert frag.findings == []


def test_typecheck_argv(tmp_path, monkeypatch):
    _tsconfig(tmp_path)
    seen = []

    def _capture(cmd, *_args, **_kwargs):
        seen.append(cmd)
        return CmdResult(rc=0, out="", duration_s=0.1)

    monkeypatch.setattr(basemod, "run", _capture)
    NpmEngine({}).typecheck(_ctx(tmp_path))
    assert seen == [
        ["npx", "--no-install", "tsc", "--noEmit", "--pretty", "false", "-p", "tsconfig.json"]
    ]


# ── unit-test ────────────────────────────────────────────────────────────────


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


def test_unit_test_understands_node_builtin_runner(tmp_path, monkeypatch):
    # `node --test` prints "ℹ pass 7", not "7 passed" — reporting "no tests reported"
    # for a suite that ran is worse than useless.
    out = "ℹ tests 7\nℹ suites 0\nℹ pass 7\nℹ fail 0\nℹ duration_ms 46.8\n"
    _package_json(tmp_path, {"test": "node --test"})
    monkeypatch.setattr(basemod, "run", _fixed(0, out))
    frag = NpmEngine({}).unit_test(_ctx(tmp_path))
    assert frag.status == Status.success
    assert frag.summary == "7 passed"


def test_unit_test_node_runner_failure(tmp_path, monkeypatch):
    out = "ℹ tests 7\nℹ pass 5\nℹ fail 2\n"
    _package_json(tmp_path, {"test": "node --test"})
    monkeypatch.setattr(basemod, "run", _fixed(1, out))
    frag = NpmEngine({}).unit_test(_ctx(tmp_path))
    assert frag.summary == "2 failed · 5 passed"


def test_unit_test_zero_counts_are_not_reported(tmp_path, monkeypatch):
    # "0 failed" is noise; only non-zero counts belong in a one-line summary.
    _package_json(tmp_path, {"test": "node --test"})
    monkeypatch.setattr(basemod, "run", _fixed(0, "ℹ pass 3\nℹ fail 0\n"))
    frag = NpmEngine({}).unit_test(_ctx(tmp_path))
    assert frag.summary == "3 passed"
