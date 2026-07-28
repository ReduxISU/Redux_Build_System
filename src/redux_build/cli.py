from __future__ import annotations

from pathlib import Path

import typer

from redux_build import __version__
from redux_build import config as config_mod
from redux_build import report as report_mod
from redux_build.context import RunContext
from redux_build.models import Fragment, Status
from redux_build.registry import UnknownEngine, get_engine

app = typer.Typer(
    help="Redux Build System — CI/CD engine CLI (rbs).",
    no_args_is_help=True,
    add_completion=False,
)

VariantOption = typer.Option(
    "", "--variant", help="Matrix variant label (e.g. py3.12)."
)


def _version_cb(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_cb,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    pass


def _resolve_engine(ctx: RunContext):
    config = config_mod.load(ctx.cwd)
    return get_engine(config_mod.engine_name(config), config)


def _run(operation: str, variant: str) -> None:
    ctx = RunContext.detect(variant=variant)
    try:
        engine = _resolve_engine(ctx)
    except (config_mod.ConfigError, UnknownEngine) as exc:
        typer.secho(f"rbs: {exc}", fg="red", err=True)
        raise typer.Exit(code=2) from None
    blocker = report_mod.find_blocker(
        ctx.report_dir, engine.requires.get(operation, [])
    )
    fragment = (
        Fragment(
            engine=engine.name,
            operation=operation,
            status=Status.blocked,
            summary=f"not run — `{blocker}` failed",
            variant=variant,
        )
        if blocker
        else engine.run_operation(operation, ctx)
    )
    report_mod.emit(ctx, fragment)
    if not fragment.ok:
        raise typer.Exit(code=1)


@app.command()
def audit(variant: str = VariantOption) -> None:
    """Dependency vulnerability audit."""
    _run("audit", variant)


@app.command("format-check")
def format_check(variant: str = VariantOption) -> None:
    """Verify formatting without writing."""
    _run("format-check", variant)


@app.command()
def lint(variant: str = VariantOption) -> None:
    """Static lint."""
    _run("lint", variant)


@app.command("unit-test")
def unit_test(variant: str = VariantOption) -> None:
    """Unit tests with coverage."""
    _run("unit-test", variant)


@app.command()
def build(variant: str = VariantOption) -> None:
    """Build the deployable artifact locally."""
    _run("build", variant)


@app.command("integration-test")
def integration_test(variant: str = VariantOption) -> None:
    """Run integration tests against the locally-built artifact."""
    _run("integration-test", variant)


@app.command()
def push(variant: str = VariantOption) -> None:
    """Push the tested artifact to the registry."""
    _run("push", variant)


@app.command()
def report(
    post: bool = typer.Option(
        False, "--post", help="Upsert the report as a sticky PR comment."
    ),
    soft: bool = typer.Option(
        False, "--soft", help="Exit 0 even if an operation failed."
    ),
) -> None:
    """Aggregate operation fragments into one report."""
    ctx = RunContext.detect()
    fragments = report_mod.load_fragments(ctx.report_dir)
    body = report_mod.render_markdown(fragments, ctx)
    Path("report.md").write_text(body)
    typer.echo(body)
    report_mod.write_step_summary(ctx, body)
    if post:
        typer.echo(report_mod.post_comment(body, ctx))
    if not soft and report_mod.has_failure(fragments):
        raise typer.Exit(code=1)
