from __future__ import annotations

import re
import tempfile
from pathlib import Path

from redux_build.context import RunContext
from redux_build.engines.base import Engine
from redux_build.models import Fragment
from redux_build.text import search


class UvEngine(Engine):
    """uv / Python toolchain (quantumsolver and the hub itself)."""

    name = "uv"
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
        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", dir=ctx.cwd, delete=False
        ) as handle:
            req = Path(handle.name)
        try:
            export = self._exec(
                [
                    "uv",
                    "export",
                    "--frozen",
                    "--no-dev",
                    "--no-emit-project",
                    "--no-hashes",
                    "--format",
                    "requirements-txt",
                    "--output-file",
                    str(req),
                ],
                ctx,
            )
            if not export.ok:
                return self._fragment("audit", ctx, export, "uv export failed")
            result = self._exec(["uv", "run", "pip-audit", "-r", str(req)], ctx)
        finally:
            req.unlink(missing_ok=True)
        summary = (
            "no known vulnerabilities"
            if result.ok
            else search(
                r"Found \d+ known vulnerabilit\w+", result.out, "vulnerabilities found"
            )
        )
        return self._fragment("audit", ctx, result, summary)

    def format_check(self, ctx: RunContext) -> Fragment:
        result = self._exec(["uv", "run", "black", "--check", "."], ctx)
        summary = (
            "all files formatted"
            if result.ok
            else search(
                r"\d+ files? would be reformatted", result.out, "needs formatting"
            )
        )
        return self._fragment("format-check", ctx, result, summary)

    def lint(self, ctx: RunContext) -> Fragment:
        result = self._exec(["uv", "run", "ruff", "check", "."], ctx)
        summary = (
            "0 issues"
            if result.ok
            else search(r"Found \d+ error\w*", result.out, "lint issues found")
        )
        return self._fragment("lint", ctx, result, summary)

    def variants(self, operation: str) -> list[str]:
        if operation != "unit-test":
            return []
        return [
            str(version)
            for version in self.config.get("unit-test", {}).get("python-versions", [])
        ]

    def unit_test(self, ctx: RunContext) -> Fragment:
        cmd = ["uv", "run"]
        python = _python_version(ctx.variant)
        if python:
            # uv fetches the interpreter on demand, so one container covers the whole version
            # matrix — no second devcontainer build per leg.
            cmd += ["--python", python]
        cmd += ["pytest", "-v"]
        package = self.config.get("package")
        if package:
            cov_min = self.config.get("unit-test", {}).get("coverage-min", 0)
            cmd += [f"--cov={package}", f"--cov-fail-under={cov_min}"]
        result = self._exec(cmd, ctx)
        return self._fragment("unit-test", ctx, result, _test_summary(result.out))


def _python_version(variant: str) -> str:
    """Interpreter a variant selects: `3.12` and `py3.12` both mean 3.12.

    Any other label (a plain matrix tag) selects nothing and the default interpreter is used.
    """
    match = re.fullmatch(r"(?:py)?(\d+\.\d+)", variant.strip())
    return match.group(1) if match else ""


def _test_summary(out: str) -> str:
    passed = search(r"\d+ passed", out, "")
    failed = search(r"\d+ failed", out, "")
    parts = [part for part in (failed, passed) if part]
    text = " · ".join(parts) if parts else "no tests reported"
    coverage = re.search(r"TOTAL.*?(\d+)%", out)
    if coverage:
        text += f" · coverage {coverage.group(1)}%"
    return text
