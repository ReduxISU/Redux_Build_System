from redux_build import report
from redux_build.context import RunContext
from redux_build.models import Fragment, Status


def _ctx(tmp_path, **env):
    base = {"RBS_REPORT_DIR": str(tmp_path / "r")}
    base.update(env)
    return RunContext(cwd=tmp_path, is_github=bool(env), env=base)


def test_write_and_load_roundtrip(tmp_path):
    ctx = _ctx(tmp_path)
    frag = Fragment(
        engine="uv", operation="lint", status=Status.success, summary="0 issues"
    )
    report.write_fragment(ctx, frag)
    loaded = report.load_fragments(ctx.report_dir)
    assert len(loaded) == 1
    assert loaded[0].operation == "lint"
    assert loaded[0].status == Status.success


def test_variant_in_fragment_filename(tmp_path):
    ctx = _ctx(tmp_path)
    frag = Fragment(
        engine="uv", operation="unit-test", status=Status.success, variant="py3.12"
    )
    path = report.write_fragment(ctx, frag)
    assert path.name == "unit-test-py3.12.json"


def test_render_markdown_overall():
    frags = [
        Fragment(engine="uv", operation="lint", status=Status.success, summary="ok"),
        Fragment(
            engine="uv",
            operation="unit-test",
            status=Status.failure,
            summary="1 failed",
        ),
    ]
    md = report.render_markdown(frags)
    assert report.MARKER in md
    assert "1 passed · 1 failed · 0 skipped" in md
    assert report.has_failure(frags)


def test_emit_writes_step_summary_and_fragment(tmp_path):
    summary = tmp_path / "summary.md"
    ctx = _ctx(tmp_path, GITHUB_STEP_SUMMARY=str(summary))
    frag = Fragment(
        engine="uv", operation="lint", status=Status.success, summary="0 issues"
    )
    report.emit(ctx, frag)
    assert "lint" in summary.read_text()
    assert (tmp_path / "r" / "lint.json").is_file()


def test_post_comment_without_context_is_noop(tmp_path):
    ctx = RunContext(cwd=tmp_path, is_github=False, env={})
    assert "skipped" in report.post_comment("body", ctx)


def test_rows_follow_pipeline_order_not_alphabetical():
    # build must not appear above the lint failure that should have gated it.
    frags = [
        Fragment(engine="npm", operation=op, status=Status.success)
        for op in ("push", "build", "lint", "audit", "unit-test")
    ]
    rows = [
        line
        for line in report.render_markdown(frags).splitlines()
        if line.startswith("| ")
    ]
    operations = [row.split("|")[1].strip() for row in rows if "Operation" not in row]
    assert operations == ["audit", "lint", "unit-test", "build", "push"]


def test_unknown_engine_falls_back_to_alphabetical():
    frags = [
        Fragment(engine="mystery", operation=op, status=Status.success)
        for op in ("zeta", "alpha")
    ]
    md = report.render_markdown(frags)
    assert md.index("alpha") < md.index("zeta")


def test_blocked_is_tallied_separately():
    frags = [
        Fragment(engine="npm", operation="build", status=Status.failure),
        Fragment(
            engine="npm",
            operation="integration-test",
            status=Status.blocked,
            summary="not run — `build` failed",
        ),
    ]
    md = report.render_markdown(frags)
    assert "⛔" in md
    assert "1 blocked" in md
    assert "0 passed · 1 failed" in md


def test_blocked_omitted_from_tally_when_none():
    frags = [Fragment(engine="npm", operation="lint", status=Status.success)]
    assert "blocked" not in report.render_markdown(frags)


def test_duration_column_rendered():
    frags = [
        Fragment(engine="npm", operation="lint", status=Status.success, duration_s=4.5),
        Fragment(engine="npm", operation="audit", status=Status.skipped),
    ]
    md = report.render_markdown(frags)
    assert "4.5s" in md
    assert "| — |" in md


def test_report_writes_rendered_table_to_step_summary(tmp_path):
    summary = tmp_path / "summary.md"
    ctx = _ctx(tmp_path, GITHUB_STEP_SUMMARY=str(summary))
    body = report.render_markdown(
        [
            Fragment(
                engine="npm",
                operation="lint",
                status=Status.success,
                summary="0 problems",
            )
        ]
    )
    report.write_step_summary(ctx, body)
    written = summary.read_text()
    assert "| Operation | Status | Summary | Time |" in written
    assert "Overall:" in written


def test_write_step_summary_noop_outside_ci(tmp_path):
    ctx = RunContext(cwd=tmp_path, is_github=False, env={})
    report.write_step_summary(ctx, "body")  # must not raise
