from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from redux_build.context import RunContext
from redux_build.engines.base import Engine
from redux_build.models import Finding, Fragment, Status
from redux_build.runner import CmdResult
from redux_build.text import relative

CONFIGURATION = "Release"

_SEVERITY_ORDER = ["critical", "high", "moderate", "low", "error", "warning"]

# MSBuild diagnostic line, emitted identically by `dotnet build` and `dotnet format`:
#   src/File.cs(46,25): error WHITESPACE: Fix whitespace formatting. [/src/proj.csproj]
_DIAGNOSTIC = re.compile(
    r"^\s*(?P<file>[^\s(][^(]*)\((?P<line>\d+),\d+\):\s+"
    r"(?P<severity>error|warning)\s+(?P<code>[^:\s]+):\s+(?P<message>.*)$",
    re.MULTILINE,
)
_PROJECT_SUFFIX = re.compile(r"\s*\[[^\]]*\]\s*$")


class DotnetEngine(Engine):
    """.NET SDK toolchain (Redux).

    The SDK is the whole toolchain — no third-party linter or formatter to install. `lint` is
    `dotnet build`, which is a real gate here because Directory.Build.props sets
    `TreatWarningsAsErrors`; analyzer and compiler diagnostics both surface through it.
    """

    name = "dotnet"
    order = [
        "audit",
        "format-check",
        "lint",
        "unit-test",
        "build",
        "integration-test",
        "push",
    ]

    def audit(self, ctx: RunContext) -> Fragment:
        result = self._exec(
            self._dotnet(
                ctx,
                "list",
                "package",
                "--vulnerable",
                "--include-transitive",
                "--format",
                "json",
            ),
            ctx,
            echo=False,
            merge_stderr=False,
        )
        findings = _safe(_audit_findings, result.out)
        # `dotnet list package` exits 0 whether or not it found anything, so the document —
        # not the exit code — decides.
        status = Status.failure if findings else None
        summary = (
            "no known vulnerabilities" if not findings else _severity_summary(findings)
        )
        return self._fragment("audit", ctx, result, summary, findings, status)

    def format_check(self, ctx: RunContext) -> Fragment:
        result = self._exec(
            self._dotnet(ctx, "format", "--verify-no-changes"), ctx, echo=False
        )
        findings = _diagnostics(result.out, ctx.cwd)
        summary = (
            "all files formatted" if not findings else _file_summary(findings, "need")
        )
        return self._fragment("format-check", ctx, result, summary, findings)

    def lint(self, ctx: RunContext) -> Fragment:
        result = self._exec(
            self._dotnet(ctx, "build", "-c", CONFIGURATION), ctx, echo=False
        )
        findings = _diagnostics(result.out, ctx.cwd)
        return self._fragment(
            "lint", ctx, result, _lint_summary(result, findings), findings
        )

    def unit_test(self, ctx: RunContext) -> Fragment:
        result = self._exec(self._dotnet(ctx, "test", "-c", CONFIGURATION), ctx)
        return self._fragment("unit-test", ctx, result, _test_summary(result.out))

    def _dotnet(self, ctx: RunContext, verb: str, *args: str) -> list[str]:
        """`dotnet <verb> <solution> …` — the solution is named explicitly because a bare
        invocation errors MSB1011 when a .csproj sits beside the solution file, as it does here.
        """
        return ["dotnet", verb, _solution(self.config, ctx), *args]


def _solution(config: dict, ctx: RunContext) -> str:
    configured = config.get("solution")
    if configured:
        return configured
    for pattern in ("*.slnx", "*.sln"):
        matches = sorted(ctx.cwd.glob(pattern))
        if matches:
            return matches[0].name
    return "."


def _safe(parser, raw: str) -> list[Finding]:
    """A malformed tool document must not crash the run."""
    try:
        return parser(raw)
    except (json.JSONDecodeError, AttributeError, TypeError, KeyError):
        return []


def _audit_findings(raw: str) -> list[Finding]:
    findings = []
    for project in json.loads(raw).get("projects", []):
        for framework in project.get("frameworks") or []:
            for kind in ("topLevelPackages", "transitivePackages"):
                for package in framework.get(kind) or []:
                    findings += _package_findings(package)
    return _by_severity(findings)


def _package_findings(package: dict) -> list[Finding]:
    version = package.get("resolvedVersion", "")
    return [
        Finding(
            message=f"vulnerable dependency `{package.get('id', '')}`",
            location=f"{package.get('id', '')}@{version}",
            rule=vulnerability.get("advisoryurl", ""),
            severity=(vulnerability.get("severity") or "").lower(),
        )
        for vulnerability in package.get("vulnerabilities") or []
    ]


def _diagnostics(out: str, cwd: Path) -> list[Finding]:
    """MSBuild repeats a diagnostic once per referencing project, so dedupe on location."""
    seen = {}
    for match in _DIAGNOSTIC.finditer(out):
        location = f"{relative(match['file'].strip(), cwd)}:{match['line']}"
        key = (location, match["code"])
        seen.setdefault(
            key,
            Finding(
                message=_PROJECT_SUFFIX.sub("", match["message"]).strip(),
                location=location,
                rule=match["code"],
                severity=match["severity"],
            ),
        )
    return _by_severity(list(seen.values()))


def _by_severity(findings: list[Finding]) -> list[Finding]:
    def key(finding: Finding) -> tuple:
        rank = (
            _SEVERITY_ORDER.index(finding.severity)
            if finding.severity in _SEVERITY_ORDER
            else len(_SEVERITY_ORDER)
        )
        return (rank, finding.location)

    return sorted(findings, key=key)


def _severity_summary(findings: list[Finding]) -> str:
    tally = Counter(finding.severity or "unknown" for finding in findings)
    ordered = sorted(
        tally.items(),
        key=lambda item: (
            _SEVERITY_ORDER.index(item[0])
            if item[0] in _SEVERITY_ORDER
            else len(_SEVERITY_ORDER)
        ),
    )
    return " · ".join(f"{count} {severity}" for severity, count in ordered)


def _file_summary(findings: list[Finding], verb: str) -> str:
    files = {finding.location.rsplit(":", 1)[0] for finding in findings}
    return f"{len(files)} file{'s' if len(files) != 1 else ''} {verb} formatting"


def _lint_summary(result: CmdResult, findings: list[Finding]) -> str:
    tally = Counter(finding.severity for finding in findings)
    parts = [
        f"{tally[name]} {name}{'s' if tally[name] != 1 else ''}"
        for name in ("error", "warning")
        if tally[name]
    ]
    if parts:
        return ", ".join(parts)
    return "0 issues" if result.ok else "build failed"


def _test_summary(out: str) -> str:
    # "Passed!  - Failed:     0, Passed:   680, Skipped:     0, Total:   680, Duration: 4 s"
    parts = []
    for label in ("Failed", "Passed", "Skipped"):
        match = re.search(rf"{label}:\s+(\d+)", out)
        count = int(match.group(1)) if match else 0
        if count:
            parts.append(f"{count} {label.lower()}")
    return " · ".join(parts) if parts else "no tests reported"
