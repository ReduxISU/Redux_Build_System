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
