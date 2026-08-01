from __future__ import annotations

from redux_build import docker
from redux_build.context import RunContext
from redux_build.models import Finding, Fragment, Status
from redux_build.runner import CmdResult, run


class Engine:
    """A toolchain module. Concrete engines implement the operations they support;
    unimplemented operations report `skipped` rather than fail.

    Toolchain operations (audit/format-check/lint/unit-test) are overridden per engine.
    Container operations (build/integration-test/push) are identical across toolchains and
    are implemented here; an engine may still override them."""

    name: str = ""
    order: list[str] = []

    # Operations that cannot run unless their prerequisites succeeded — you cannot test an
    # artifact that failed to build, or push one that failed its tests. Quality gates
    # (audit/format-check/lint/unit-test) deliberately have no prerequisites: they always run
    # so one CI pass reports every problem at once.
    requires: dict[str, list[str]] = {
        "integration-test": ["build"],
        "push": ["build", "integration-test"],
    }

    def __init__(self, config: dict):
        self.config = config

    def variants(self, operation: str) -> list[str]:
        """Labels this operation repeats over — e.g. interpreter versions for a test matrix.

        Empty means run it once. Fanning out here rather than in a workflow matrix keeps the
        whole run inside one container instead of rebuilding the image per leg.
        """
        return []

    def skipped(self, operation: str, reason: str, ctx: RunContext) -> Fragment:
        # ctx supplies the variant: fragment filenames are keyed on it, so a skipped
        # operation without one would clobber its sibling across a matrix run.
        return Fragment(
            engine=self.name,
            operation=operation,
            status=Status.skipped,
            summary=reason,
            variant=ctx.variant,
        )

    def audit(self, ctx: RunContext) -> Fragment:
        return self.skipped("audit", "not implemented", ctx)

    def format_check(self, ctx: RunContext) -> Fragment:
        return self.skipped("format-check", "not implemented", ctx)

    def lint(self, ctx: RunContext) -> Fragment:
        return self.skipped("lint", "not implemented", ctx)

    def unit_test(self, ctx: RunContext) -> Fragment:
        return self.skipped("unit-test", "not implemented", ctx)

    def build(self, ctx: RunContext) -> Fragment:
        artifact = self.config.get("artifact", {})
        dockerfile = ctx.cwd / artifact.get("dockerfile", "Dockerfile")
        if not dockerfile.is_file():
            return self.skipped("build", f"no {dockerfile.name}", ctx)
        tag = docker.local_tag(self.config)
        cmd = ["docker", "buildx", "build", "--load", "-f", str(dockerfile), "-t", tag]
        if ctx.is_github:
            cmd += ["--cache-from", "type=gha", "--cache-to", "type=gha,mode=max"]
        cmd.append(artifact.get("context", "."))
        result = self._exec(cmd, ctx)
        if not result.ok:
            return self._fragment("build", ctx, result, "docker build failed")
        size = docker.image_size(ctx, tag)
        summary = f"built `{tag}`" + (f" · {size}" if size else "")
        return self._fragment("build", ctx, result, summary)

    def integration_test(self, ctx: RunContext) -> Fragment:
        return self.skipped("integration-test", "not implemented", ctx)

    def push(self, ctx: RunContext) -> Fragment:
        return self.skipped("push", "not implemented", ctx)

    def run_operation(self, operation: str, ctx: RunContext) -> Fragment:
        return getattr(self, operation.replace("-", "_"))(ctx)

    def _exec(
        self,
        cmd: list[str],
        ctx: RunContext,
        echo: bool = True,
        merge_stderr: bool = True,
    ) -> CmdResult:
        """Run a tool. `echo=False` for JSON invocations — the reporter prints the parsed
        findings instead, so logs stay readable rather than dumping a raw document."""
        result = run(cmd, ctx.cwd, merge_stderr=merge_stderr)
        if echo and result.out.strip():
            print(result.out.rstrip("\n"))
        return result

    def _fragment(
        self,
        operation: str,
        ctx: RunContext,
        result: CmdResult,
        summary: str,
        findings: list[Finding] | None = None,
        status: Status | None = None,
        metrics: dict | None = None,
    ) -> Fragment:
        # `status` overrides the exit code for tools that report problems but still exit 0 —
        # `dotnet list package --vulnerable` is one.
        status = status or (Status.success if result.ok else Status.failure)
        return Fragment(
            engine=self.name,
            operation=operation,
            status=status,
            summary=summary,
            variant=ctx.variant,
            duration_s=result.duration_s,
            findings=findings or [],
            metrics=metrics or {},
        )
