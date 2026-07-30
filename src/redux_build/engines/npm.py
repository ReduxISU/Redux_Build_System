from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from redux_build.context import RunContext
from redux_build.engines.base import Engine
from redux_build.models import Finding, Fragment
from redux_build.runner import CmdResult
from redux_build.text import search

_SEVERITY_ORDER = ["critical", "high", "moderate", "low", "error", "warning", "info"]

_BIOME_LABELS = {
    "format": "format",
    "assist/source/organizeImports": "import order",
}


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
            ["npx", "--no-install", "eslint", ".", "-f", "json"], ctx, echo=False
        )
        findings = _parse(result, lambda raw: _eslint_findings(raw, ctx.cwd))
        return self._fragment(
            "lint", ctx, result, _lint_summary(result, findings), findings
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
            location=f"{_relative(entry.get('filePath', ''), cwd)}:{message.get('line', 0)}",
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
    passed = _test_count(out, r"(\d+) passed", r"^[^\d\n]*pass (\d+)")
    failed = _test_count(out, r"(\d+) failed", r"^[^\d\n]*fail (\d+)")
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


def _relative(path: str, cwd: Path) -> str:
    try:
        return str(Path(path).relative_to(cwd))
    except ValueError:
        return path


def _as_text(value) -> str:
    return value if isinstance(value, str) else str(value)
