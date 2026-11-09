from __future__ import annotations

import json
import re
import tempfile
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree

from redux_build.context import RunContext
from redux_build.engines.base import Engine
from redux_build.models import Finding, Fragment, Status
from redux_build.runner import CmdResult
from redux_build.text import mtp_counts, relative

CONFIGURATION = "Release"

# `[unit-test] runner`: "auto" reads global.json (see uses_mtp); the others force a runner.
RUNNERS = ("auto", "vstest", "mtp")
# What an MTP repo references for coverage, and what MTP says when a project lacks it.
MTP_COVERAGE_PACKAGE = "Microsoft.Testing.Extensions.CodeCoverage"
_MTP_NO_COVERAGE = "Unknown option '--coverage'"
_MTP_RUNNER = "microsoft.testing.platform"
_PROJECT_SUFFIXES = (".csproj", ".fsproj", ".vbproj")

_SEVERITY_ORDER = ["critical", "high", "moderate", "low", "error", "warning"]

# MSBuild diagnostic line, emitted identically by `dotnet build` and `dotnet format`:
#   src/File.cs(46,25): error WHITESPACE: Fix whitespace formatting. [/src/proj.csproj]
# Held to one line ([ \t]*, [^(\n]*): otherwise the file name can begin on the build line before
# it ("API -> …/API.dll" + newline + "src/File.cs"), so each copy MSBuild prints gets its own
# location and survives the dedupe in _diagnostics (#8).
_DIAGNOSTIC = re.compile(
    r"^[ \t]*(?P<file>[^\s(][^(\n]*)\((?P<line>\d+),\d+\):\s+"
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
        "typecheck",
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

    def typecheck(self, ctx: RunContext) -> Fragment:
        # The C# compiler is the type checker, and `lint` already runs it under
        # TreatWarningsAsErrors — a second pass would report the same diagnostics twice.
        return self.skipped("typecheck", "covered by `lint` (dotnet build)", ctx)

    def unit_test(self, ctx: RunContext) -> Fragment:
        settings = self.config.get("unit-test", {})
        runner = settings.get("runner", "auto")
        if runner not in RUNNERS:
            return self._fragment(
                "unit-test",
                ctx,
                CmdResult(rc=1, out="", duration_s=0.0),
                f"unknown `[unit-test] runner` {runner!r}; use "
                + ", ".join(f"`{name}`" for name in RUNNERS),
            )
        if runner == "mtp" or (runner == "auto" and uses_mtp(ctx.cwd)):
            return self._unit_test_mtp(ctx)
        # Coverage goes to a fresh temp dir so a previous run's report can never be counted;
        # instrumentation needs a real build, so this deliberately does not pass --no-build.
        with tempfile.TemporaryDirectory() as results:
            result = self._exec(
                self._dotnet(
                    ctx,
                    "test",
                    "-c",
                    CONFIGURATION,
                    "--collect",
                    "XPlat Code Coverage",
                    "--results-directory",
                    results,
                    *self._filter(),
                ),
                ctx,
            )
            coverage = _coverage(Path(results))
        return self._unit_test_fragment(
            ctx, result, _test_summary(result.out), coverage
        )

    def _unit_test_mtp(self, ctx: RunContext) -> Fragment:
        """`unit-test` for a repo whose global.json selects Microsoft Testing Platform.

        In that mode `dotnet test` takes the solution as `--solution` (a bare path is refused),
        and VSTest's `--collect` is an unknown option; coverage comes from the
        Microsoft.Testing.Extensions.CodeCoverage extension instead. `--no-ansi` because MTP
        writes colour codes even into a pipe when CI=true, and `--no-progress` keeps its live
        progress line out of the log. No `--coverage-output`: with it every
        test project would write the same file name.
        """
        coverage_args = ["--coverage", "--coverage-output-format", "cobertura"]
        note = ""
        with tempfile.TemporaryDirectory() as results:
            cmd = [
                "dotnet",
                "test",
                *_mtp_target(_solution(self.config, ctx)),
                "-c",
                CONFIGURATION,
                "--no-ansi",
                "--no-progress",
                "--results-directory",
                results,
            ]
            result = self._exec([*cmd, *coverage_args, *self._filter()], ctx)
            if _MTP_NO_COVERAGE in result.out:
                # A project without the extension rejects --coverage and runs nothing. Like a
                # VSTest project without coverlet.collector, it then gets no coverage gate, so
                # run again without the flags and say why coverage is missing.
                first = result
                result = self._exec([*cmd, *self._filter()], ctx)
                result.duration_s = round(result.duration_s + first.duration_s, 2)
                note = f" · no coverage: reference `{MTP_COVERAGE_PACKAGE}`"
            coverage = _coverage(Path(results), "*.cobertura.xml")
        return self._unit_test_fragment(
            ctx, result, _mtp_summary(result.out) + note, coverage
        )

    def _filter(self) -> list[str]:
        """`--filter <expression>` from `[unit-test] filter`, or nothing when it is unset.

        The same flag in both runners: xunit.v3 (and MSTest/NUnit) accept the VSTest filter
        syntax under Microsoft Testing Platform too, so `Category!=Performance` needs no
        translation (xunit's own spelling of it there is `--filter-not-trait Category=Performance`).
        """
        expression = self.config.get("unit-test", {}).get("filter")
        return ["--filter", expression] if expression else []

    def _unit_test_fragment(
        self,
        ctx: RunContext,
        result: CmdResult,
        summary: str,
        coverage: float | None,
    ) -> Fragment:
        minimum = self.config.get("unit-test", {}).get("coverage-min", 0)
        status = None
        if coverage is not None:
            summary += f" · coverage {coverage:.0f}%"
            if coverage < minimum:
                summary += f" (min {minimum}%)"
                status = Status.failure
        return self._fragment(
            "unit-test",
            ctx,
            result,
            summary,
            status=status,
            metrics={"coverage": round(coverage, 1)} if coverage is not None else {},
        )

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


def _mtp_target(solution: str) -> list[str]:
    """How MTP-mode `dotnet test` is pointed at what to run: `--project` for a project file,
    `--solution` otherwise, and nothing at all when no solution was found (it then looks in the
    working directory itself, as a bare `dotnet test` does)."""
    if solution == ".":
        return []
    if Path(solution).suffix.lower() in _PROJECT_SUFFIXES:
        return ["--project", solution]
    return ["--solution", solution]


def uses_mtp(cwd: Path) -> bool:
    """Whether `dotnet test` run in `cwd` is in Microsoft Testing Platform mode.

    Resolved as the SDK does: the nearest global.json at or above `cwd` decides, even when it
    has no `test` section, and the mode is MTP when its `test.runner` is
    `Microsoft.Testing.Platform`. Keys are matched exactly and the value case-insensitively;
    comments and trailing commas are allowed. An unreadable file means VSTest, today's
    behaviour.
    """
    for folder in (cwd, *cwd.parents):
        path = folder / "global.json"
        if path.is_file():
            try:
                document = json.loads(
                    _strip_jsonc(path.read_text(encoding="utf-8-sig"))
                )
            except (OSError, ValueError):
                return False
            test = document.get("test") if isinstance(document, dict) else None
            runner = test.get("runner") if isinstance(test, dict) else None
            return isinstance(runner, str) and runner.lower() == _MTP_RUNNER
    return False


_JSON_STRING = r'(?P<string>"(?:[^"\\]|\\.)*")'
_JSONC_COMMENT = re.compile(_JSON_STRING + r"|//[^\n]*|/\*.*?\*/", re.S)
_JSONC_TRAILING_COMMA = re.compile(_JSON_STRING + r"|,(?=\s*[}\]])")


def _strip_jsonc(text: str) -> str:
    """global.json may carry `//` and `/* */` comments and trailing commas; strings are kept.
    Comments go first, so a comma followed by a comment and then `}` is still trailing.
    """
    for pattern in (_JSONC_COMMENT, _JSONC_TRAILING_COMMA):
        text = pattern.sub(lambda match: match["string"] or "", text)
    return text


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


def _coverage(results: Path, name: str = "coverage.cobertura.xml") -> float | None:
    """Line coverage across every cobertura report the collector wrote.

    Summed rather than averaged so several test projects merge correctly. None when nothing was
    produced — a project without `coverlet.collector` simply gets no coverage gate. `name` is
    the file pattern: coverlet writes `<guid>/coverage.cobertura.xml`, while MTP's CodeCoverage
    extension writes `<guid>.cobertura.xml` straight into the results directory.
    """
    covered = valid = 0
    for report in results.rglob(name):
        try:
            root = ElementTree.parse(report).getroot()
        except ElementTree.ParseError:
            continue
        covered += int(root.get("lines-covered", 0))
        valid += int(root.get("lines-valid", 0))
    return 100 * covered / valid if valid else None


def _test_summary(out: str) -> str:
    # "Passed!  - Failed:     0, Passed:   680, Skipped:     0, Total:   680, Duration: 4 s"
    parts = []
    for label in ("Failed", "Passed", "Skipped"):
        match = re.search(rf"{label}:\s+(\d+)", out)
        count = int(match.group(1)) if match else 0
        if count:
            parts.append(f"{count} {label.lower()}")
    return " · ".join(parts) if parts else "no tests reported"


def _mtp_summary(out: str) -> str:
    """The same "3 failed · 677 passed · 2 skipped" shape from MTP's `Test run summary:` block.
    MTP counts a run where no test ran as a failure (exit 8, and 1 or 5 when a project was
    refused), so that gets a summary of its own."""
    counts = mtp_counts(out)
    if counts is None:
        return "no tests reported"
    if not counts["total"]:
        return "no tests ran"
    named = (("failed", "failed"), ("succeeded", "passed"), ("skipped", "skipped"))
    return " · ".join(f"{counts[key]} {label}" for key, label in named if counts[key])
