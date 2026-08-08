from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from redux_build.context import RunContext
from redux_build.engines.base import Engine
from redux_build.models import Finding, Fragment
from redux_build.runner import CmdResult
from redux_build.text import relative, search

_SEVERITY_ORDER = ["critical", "high", "moderate", "low", "error", "warning", "info"]

_BIOME_LABELS = {
    "format": "format",
    "assist/source/organizeImports": "import order",
}

# `path(line,col): error TS2532: message`. Anchored on a non-space first character because tsc
# indents a diagnostic's elaboration underneath it — those lines belong to the finding above,
# not to one of their own.
# vitest prints `Test Files 13 passed` *above* `Tests 235 passed`, and jest writes `Tests:` — so a
# bare `(\d+) passed` finds the file count and reports 13 tests where 235 ran. Anchoring on the
# Tests line first is what makes the number mean tests. Tried before the looser patterns, never
# instead of them: a runner that prints neither still falls through to those.
_TESTS_LINE_PASSED = r"^[ \t]*Tests:?[ \t]+.*?(\d+) passed"
_TESTS_LINE_FAILED = r"^[ \t]*Tests:?[ \t]+.*?(\d+) failed"

_TSC_DIAGNOSTIC = re.compile(
    r"^(?P<path>[^\s(][^(]*)\((?P<line>\d+),\d+\): "
    r"(?P<severity>error|warning) (?P<rule>TS\d+): (?P<message>.*)$",
    re.MULTILINE,
)


class NpmEngine(Engine):
    """npm / Node toolchain (Redux_GUI).

    Tools are invoked through `npx --no-install` — the node equivalent of `uv run` — so a
    missing devDependency fails loudly instead of being silently fetched from the registry.
    Each tool is asked for JSON so the fragment carries individual findings, not just a count.
    """

    name = "npm"
    order = [
        "audit",
        "format-check",
        "lint",
        "typecheck",
        "unit-test",
        "build",
        "integration-test",
        "push",
    ]

    def audit(self, ctx: RunContext) -> Fragment:
        # --omit=dev scans only what ships, matching the uv engine's --no-dev export.
        result = self._exec(
            ["npm", "audit", "--omit=dev", "--json"],
            ctx,
            echo=False,
            merge_stderr=False,
        )
        findings = _parse(result, _audit_findings)
        summary = (
            "no known vulnerabilities" if not findings else _severity_summary(findings)
        )
        return self._fragment("audit", ctx, result, summary, findings)

    def format_check(self, ctx: RunContext) -> Fragment:
        result = self._exec(
            ["npx", "--no-install", "biome", "check", ".", "--reporter=json"],
            ctx,
            echo=False,
            merge_stderr=False,
        )
        findings = _parse(result, _biome_findings)
        summary = "all files formatted" if not findings else _biome_summary(findings)
        return self._fragment("format-check", ctx, result, summary, findings)

    def lint(self, ctx: RunContext) -> Fragment:
        result = self._exec(
            ["npx", "--no-install", "eslint", ".", "-f", "json"],
            ctx,
            echo=False,
            merge_stderr=False,
        )
        findings = _parse(result, lambda raw: _eslint_findings(raw, ctx.cwd))
        return self._fragment(
            "lint", ctx, result, _lint_summary(result, findings), findings
        )

    def typecheck(self, ctx: RunContext) -> Fragment:
        # A root tsconfig.json is what makes a repo type-checkable. A plain-JS repo reports
        # `skipped` rather than passing vacuously — the same policy `unit-test` applies to a
        # missing `test` script, and it activates on its own the moment types arrive.
        if not (ctx.cwd / "tsconfig.json").is_file():
            return self.skipped("typecheck", "no tsconfig.json", ctx)
        # `--pretty false` because the alternative is an ANSI code frame nothing can parse. tsc
        # already drops it when stdout is a pipe; this defends against `"pretty": true` in the
        # tsconfig. stderr stays merged, unlike the JSON gates: there is no document to corrupt,
        # and an `npx: command not found` there is the only clue the summary would otherwise have.
        result = self._exec(
            ["npx", "--no-install", "tsc", "--noEmit", "--pretty", "false", "-p", "tsconfig.json"],
            ctx,
            echo=False,
        )
        findings = _tsc_findings(result.out)
        return self._fragment(
            "typecheck", ctx, result, _tsc_summary(result, findings), findings
        )

    def unit_test(self, ctx: RunContext) -> Fragment:
        if not _has_test_script(ctx):
            return self.skipped("unit-test", "no `test` script in package.json", ctx)
        result = self._exec(["npm", "test"], ctx)
        return self._fragment("unit-test", ctx, result, _test_summary(result.out))


def _parse(result: CmdResult, parser) -> list[Finding]:
    """Never let a malformed tool document crash the run — the exit code still gates."""
    try:
        return parser(result.out)
    except (json.JSONDecodeError, AttributeError, TypeError, KeyError):
        return []


def _eslint_findings(raw: str, cwd: Path) -> list[Finding]:
    findings = [
        Finding(
            message=message.get("message", ""),
            location=f"{relative(entry.get('filePath', ''), cwd)}:{message.get('line', 0)}",
            rule=message.get("ruleId") or "",
            severity="error" if message.get("severity") == 2 else "warning",
        )
        for entry in json.loads(raw)
        for message in entry.get("messages", [])
    ]
    return _by_severity(findings)


def _biome_findings(raw: str) -> list[Finding]:
    findings = [
        Finding(
            message=_biome_message(diagnostic),
            location=(diagnostic.get("location") or {}).get("path") or "",
            rule=diagnostic.get("category", ""),
            severity=diagnostic.get("severity", ""),
        )
        for diagnostic in json.loads(raw).get("diagnostics", [])
    ]
    return _by_severity(findings)


def _biome_message(diagnostic: dict) -> str:
    # Every format diagnostic carries the same boilerplate followed by the whole diff.
    if diagnostic.get("category") == "format":
        return "needs formatting"
    return _as_text(diagnostic.get("message", ""))


def _audit_findings(raw: str) -> list[Finding]:
    findings = []
    for name, vulnerability in json.loads(raw).get("vulnerabilities", {}).items():
        advisory = next(
            (via for via in vulnerability.get("via", []) if isinstance(via, dict)), {}
        )
        findings.append(
            Finding(
                message=advisory.get("title") or f"vulnerable dependency `{name}`",
                location=f"{name}@{vulnerability.get('range', '')}",
                rule=advisory.get("url", ""),
                severity=vulnerability.get("severity", ""),
            )
        )
    return _by_severity(findings)


def _tsc_findings(raw: str) -> list[Finding]:
    # Not routed through `_parse`: a regex has no malformed-document failure mode, and tsc emits
    # paths relative to the directory it ran in, which is already ctx.cwd.
    findings = [
        Finding(
            message=match["message"],
            location=f"{match['path']}:{match['line']}",
            rule=match["rule"],
            severity=match["severity"],
        )
        for match in _TSC_DIAGNOSTIC.finditer(raw)
    ]
    return _by_severity(findings)


def _tsc_summary(result: CmdResult, findings: list[Finding]) -> str:
    if findings:
        files = len({finding.location.rsplit(":", 1)[0] for finding in findings})
        return f"{_plural(len(findings), 'type error')} in {_plural(files, 'file')}"
    # tsc reports its own configuration problems (TS5058, TS6053) with no file:line to match, so
    # a non-zero exit having parsed nothing is still a failure — never a silent pass.
    return "no type errors" if result.ok else "typecheck failed"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _by_severity(findings: list[Finding]) -> list[Finding]:
    """Most severe first, so a truncated report still shows what matters."""

    def key(finding: Finding) -> tuple:
        rank = (
            _SEVERITY_ORDER.index(finding.severity)
            if finding.severity in _SEVERITY_ORDER
            else len(_SEVERITY_ORDER)
        )
        return (rank, finding.location)

    return sorted(findings, key=key)


def _severity_summary(findings: list[Finding]) -> str:
    tally = Counter(finding.severity for finding in findings)
    ordered = sorted(
        tally.items(),
        key=lambda item: (
            _SEVERITY_ORDER.index(item[0])
            if item[0] in _SEVERITY_ORDER
            else len(_SEVERITY_ORDER)
        ),
    )
    return " · ".join(f"{count} {severity}" for severity, count in ordered)


def _biome_summary(findings: list[Finding]) -> str:
    errors = [finding for finding in findings if finding.severity == "error"]
    if not errors:
        return f"{len(findings)} advisories"
    tally = Counter(_BIOME_LABELS.get(f.rule, f.rule) for f in errors)
    return " · ".join(f"{count} {label}" for label, count in tally.most_common())


def _lint_summary(result: CmdResult, findings: list[Finding]) -> str:
    if findings:
        tally = Counter(finding.severity for finding in findings)
        parts = [
            f"{tally[name]} {name}{'s' if tally[name] != 1 else ''}"
            for name in ("error", "warning")
            if tally[name]
        ]
        return ", ".join(parts)
    # Fall back to the human formatter's line if the JSON document could not be parsed.
    counts = search(r"\d+ errors?, \d+ warnings?", result.out, "")
    if counts:
        return counts
    return "0 problems" if result.ok else "lint issues found"


def _test_summary(out: str) -> str:
    # Two shapes: "7 passed" (vitest, jest) and "ℹ pass 7" (node --test).
    passed = _test_count(out, _TESTS_LINE_PASSED, r"(\d+) passed", r"^[^\d\n]*pass (\d+)")
    failed = _test_count(out, _TESTS_LINE_FAILED, r"(\d+) failed", r"^[^\d\n]*fail (\d+)")
    parts = [
        f"{count} {label}"
        for count, label in ((failed, "failed"), (passed, "passed"))
        if count
    ]
    return " · ".join(parts) if parts else "no tests reported"


def _test_count(out: str, *patterns: str) -> int:
    for pattern in patterns:
        match = re.search(pattern, out, re.MULTILINE)
        if match:
            return int(match.group(1))
    return 0


def _has_test_script(ctx: RunContext) -> bool:
    path = ctx.cwd / "package.json"
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return False
    return bool(data.get("scripts", {}).get("test"))


def _as_text(value) -> str:
    return value if isinstance(value, str) else str(value)
