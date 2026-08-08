from __future__ import annotations

import time

from redux_build import docker, stack
from redux_build.context import RunContext
from redux_build.models import Finding, Fragment, Status
from redux_build.runner import CmdResult, run
from redux_build.text import test_counts


class Engine:
    """A toolchain module. Concrete engines implement the operations they support;
    unimplemented operations report `skipped` rather than fail.

    Toolchain operations (audit/format-check/lint/typecheck/unit-test) are overridden per engine.
    Container operations (build/integration-test/push) are identical across toolchains and
    are implemented here; an engine may still override them."""

    name: str = ""
    order: list[str] = []

    # Operations that cannot run unless their prerequisites succeeded — you cannot test an
    # artifact that failed to build, or push one that failed its tests. Quality gates
    # (audit/format-check/lint/typecheck/unit-test) deliberately have no prerequisites: they always
    # run so one CI pass reports every problem at once.
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

    def typecheck(self, ctx: RunContext) -> Fragment:
        return self.skipped("typecheck", "not implemented", ctx)

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
        """Run the repo's integration suite against the image `build` just produced.

        The artifact is started in a throwaway network alongside whatever services it needs, and
        the suite is pointed at it via RBS_BASE_URL. The bytes under test are the bytes that ship
        — nothing round-trips through a registry first.
        """
        integration = self.config.get("integration", {})
        command = integration.get("command")
        if not command:
            return self.skipped("integration-test", "no [integration] command", ctx)

        services = stack.plan(self.config)
        artifact = services[-1]
        started = time.monotonic()
        topology = stack.bring_up(ctx, services, stack.run_id(self.config))
        try:
            timeout = int(integration.get("timeout", stack.DEFAULT_TIMEOUT_S))
            unready = stack.wait_until_ready(ctx, topology, services, timeout)
            if unready:
                return self._unready(unready, topology, ctx, started, timeout)
            result = self._exec(
                command,
                ctx,
                env={**ctx.env, **stack.test_env(topology, artifact)},
                shell=True,
            )
            counts = test_counts(result.out)
            summary = f"{artifact.health_path} ready" + (
                f" · {counts}" if counts else ""
            )
            return self._fragment(
                "integration-test",
                ctx,
                result,
                summary if result.ok else counts or "tests failed",
            )
        finally:
            # The only cleanup hook there is: `cli._execute` has no lifecycle, so a container
            # left behind here is left behind for good.
            stack.tear_down(ctx, topology)

    def _unready(
        self,
        name: str,
        topology: stack.Stack,
        ctx: RunContext,
        started: float,
        timeout: int,
    ) -> Fragment:
        container = f"{topology.network}-{name}"
        result = CmdResult(
            rc=1, out="", duration_s=round(time.monotonic() - started, 2)
        )
        findings = [
            Finding(message=line, location=name, severity="error")
            for line in docker.logs(ctx, container)
        ]
        reason = _unready_reason(ctx, name, container, topology, timeout)
        return self._fragment(
            "integration-test", ctx, result, f"`{name}` {reason}", findings=findings
        )

    def push(self, ctx: RunContext) -> Fragment:
        return self.skipped("push", "not implemented", ctx)

    def run_operation(self, operation: str, ctx: RunContext) -> Fragment:
        return getattr(self, operation.replace("-", "_"))(ctx)

    def _exec(
        self,
        cmd: list[str] | str,
        ctx: RunContext,
        echo: bool = True,
        merge_stderr: bool = True,
        env: dict | None = None,
        shell: bool = False,
    ) -> CmdResult:
        """Run a tool. `echo=False` for JSON invocations — the reporter prints the parsed
        findings instead, so logs stay readable rather than dumping a raw document.

        `env`/`shell` serve `integration-test`, whose command comes from rbs.toml as a string and
        needs the stack's addresses in its environment."""
        result = run(cmd, ctx.cwd, env=env, shell=shell, merge_stderr=merge_stderr)
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


def _unready_reason(
    ctx: RunContext, name: str, container: str, topology: stack.Stack, timeout: int
) -> str:
    """Why a service never answered — the three cases read very differently to whoever is
    debugging, and "not ready after 180s" on a container that died in a second is a lie.
    """
    if name not in topology.urls:
        return "failed to start"
    if not docker.is_running(ctx, container):
        return "exited before becoming ready"
    return f"not ready after {timeout}s"
