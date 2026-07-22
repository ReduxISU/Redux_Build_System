from __future__ import annotations

from redux_build import docker
from redux_build.context import RunContext
from redux_build.models import Fragment, Status
from redux_build.runner import CmdResult, run


class Engine:
    """A toolchain module. Concrete engines implement the operations they support;
    unimplemented operations report `skipped` rather than fail.

    Toolchain operations (audit/format-check/lint/unit-test) are overridden per engine.
    Container operations (build/integration-test/push) are identical across toolchains and
    are implemented here; an engine may still override them."""

    name: str = ""
    order: list[str] = []

    def __init__(self, config: dict):
        self.config = config

    def skipped(self, operation: str, reason: str) -> Fragment:
        return Fragment(
            engine=self.name,
            operation=operation,
            status=Status.skipped,
            summary=reason,
        )

    def audit(self, ctx: RunContext) -> Fragment:
        return self.skipped("audit", "not implemented")

    def format_check(self, ctx: RunContext) -> Fragment:
        return self.skipped("format-check", "not implemented")

    def lint(self, ctx: RunContext) -> Fragment:
        return self.skipped("lint", "not implemented")

    def unit_test(self, ctx: RunContext) -> Fragment:
        return self.skipped("unit-test", "not implemented")

    def build(self, ctx: RunContext) -> Fragment:
        artifact = self.config.get("artifact", {})
        dockerfile = ctx.cwd / artifact.get("dockerfile", "Dockerfile")
        if not dockerfile.is_file():
            return self.skipped("build", f"no {dockerfile.name}")
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
        return self.skipped("integration-test", "not implemented")

    def push(self, ctx: RunContext) -> Fragment:
        return self.skipped("push", "not implemented")

    def run_operation(self, operation: str, ctx: RunContext) -> Fragment:
        return getattr(self, operation.replace("-", "_"))(ctx)

    def _exec(self, cmd: list[str], ctx: RunContext) -> CmdResult:
        result = run(cmd, ctx.cwd)
        if result.out.strip():
            print(result.out.rstrip("\n"))
        return result

    def _fragment(
        self, operation: str, ctx: RunContext, result: CmdResult, summary: str
    ) -> Fragment:
        status = Status.success if result.ok else Status.failure
        return Fragment(
            engine=self.name,
            operation=operation,
            status=status,
            summary=summary,
            variant=ctx.variant,
            duration_s=result.duration_s,
        )
