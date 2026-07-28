from __future__ import annotations

import json

from redux_build.context import RunContext
from redux_build.engines.base import Engine
from redux_build.models import Fragment
from redux_build.runner import CmdResult
from redux_build.text import search


class NpmEngine(Engine):
    """npm / Node toolchain (Redux_GUI).

    Tools are invoked through `npx --no-install` — the node equivalent of `uv run` — so a
    missing devDependency fails loudly instead of being silently fetched from the registry.
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
        result = self._exec(["npm", "audit", "--omit=dev"], ctx)
        summary = (
            "no known vulnerabilities"
            if result.ok
            else search(
                r"\d+ \w+ severity vulnerabilit\w+", result.out, "vulnerabilities found"
            )
        )
        return self._fragment("audit", ctx, result, summary)

    def format_check(self, ctx: RunContext) -> Fragment:
        result = self._exec(["npx", "--no-install", "biome", "check", "."], ctx)
        summary = (
            "all files formatted"
            if result.ok
            else search(r"Found \d+ error\w*", result.out, "needs formatting")
        )
        return self._fragment("format-check", ctx, result, summary)

    def lint(self, ctx: RunContext) -> Fragment:
        result = self._exec(["npx", "--no-install", "eslint", "."], ctx)
        return self._fragment("lint", ctx, result, _lint_summary(result))

    def unit_test(self, ctx: RunContext) -> Fragment:
        if not _has_test_script(ctx):
            return self.skipped("unit-test", "no `test` script in package.json")
        result = self._exec(["npm", "test"], ctx)
        return self._fragment("unit-test", ctx, result, _test_summary(result.out))


def _lint_summary(result: CmdResult) -> str:
    # eslint exits 0 with warnings, so parse the count line before trusting the exit code.
    counts = search(r"\d+ errors?, \d+ warnings?", result.out, "")
    if counts:
        return counts
    return "0 problems" if result.ok else "lint issues found"


def _test_summary(out: str) -> str:
    passed = search(r"\d+ passed", out, "")
    failed = search(r"\d+ failed", out, "")
    parts = [part for part in (failed, passed) if part]
    return " · ".join(parts) if parts else "no tests reported"


def _has_test_script(ctx: RunContext) -> bool:
    path = ctx.cwd / "package.json"
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return False
    return bool(data.get("scripts", {}).get("test"))
