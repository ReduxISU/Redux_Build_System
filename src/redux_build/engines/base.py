from __future__ import annotations

from redux_build.context import RunContext
from redux_build.models import Fragment, Status


class Engine:
    """A toolchain module. Concrete engines implement the operations they support;
    unimplemented operations report `skipped` rather than fail."""

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
        return self.skipped("build", "not implemented")

    def integration_test(self, ctx: RunContext) -> Fragment:
        return self.skipped("integration-test", "not implemented")

    def push(self, ctx: RunContext) -> Fragment:
        return self.skipped("push", "not implemented")

    def run_operation(self, operation: str, ctx: RunContext) -> Fragment:
        return getattr(self, operation.replace("-", "_"))(ctx)
