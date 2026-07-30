from redux_build import report
from redux_build.context import RunContext
from redux_build.models import Finding, Fragment, Status


def _with_findings(count, **overrides):
    findings = [
        Finding(
            message=overrides.get("message", f"problem {i}"),
            location=f"src/file{i}.js:{i}",
            rule="no-undef",
            severity="error",
        )
        for i in range(count)
    ]
    return Fragment(
        engine="npm",
        operation="lint",
        status=Status.failure,
        summary=f"{count} errors",
        findings=findings,
    )


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


def test_pr_number_from_explicit_env(tmp_path):
    # devcontainers/ci cannot see GITHUB_EVENT_PATH, so the workflow passes the number in.
    ctx = RunContext(cwd=tmp_path, is_github=True, env={"RBS_PR_NUMBER": "137"})
    assert report.pull_request_number(ctx) == 137


def test_pr_number_from_event_payload(tmp_path):
    event = tmp_path / "event.json"
    event.write_text('{"pull_request": {"number": 42}}')
    ctx = RunContext(
        cwd=tmp_path, is_github=True, env={"GITHUB_EVENT_PATH": str(event)}
    )
    assert report.pull_request_number(ctx) == 42


def test_pr_number_none_on_push_event(tmp_path):
    event = tmp_path / "event.json"
    event.write_text('{"ref": "refs/heads/main"}')
    ctx = RunContext(
        cwd=tmp_path, is_github=True, env={"GITHUB_EVENT_PATH": str(event)}
    )
    assert report.pull_request_number(ctx) is None


def test_pr_number_tolerates_unreadable_event_path(tmp_path):
    ctx = RunContext(
        cwd=tmp_path, is_github=True, env={"GITHUB_EVENT_PATH": "/nope/x.json"}
    )
    assert report.pull_request_number(ctx) is None


def test_pr_number_ignores_non_numeric_env(tmp_path):
    ctx = RunContext(cwd=tmp_path, is_github=True, env={"RBS_PR_NUMBER": ""})
    assert report.pull_request_number(ctx) is None


def test_step_summary_failure_does_not_raise(tmp_path):
    ctx = RunContext(
        cwd=tmp_path, is_github=True, env={"GITHUB_STEP_SUMMARY": "/nope/deep/sum.md"}
    )
    report.write_step_summary(ctx, "body")  # must not raise


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


def test_findings_render_in_a_collapsed_block():
    md = report.render_markdown([_with_findings(2)])
    assert "<details><summary>" in md
    assert "| Severity | Location | Rule | Message |" in md
    assert "src/file0.js:0" in md
    assert "</details>" in md


def test_no_details_block_without_findings():
    frags = [Fragment(engine="npm", operation="lint", status=Status.success)]
    assert "<details>" not in report.render_markdown(frags)


def test_findings_truncated_with_the_remainder_stated():
    # Never silently cap: the reader must know how much was withheld.
    md = report.render_markdown([_with_findings(report.MAX_FINDINGS + 7)])
    assert "_… and 7 more_" in md
    assert md.count("src/file") == report.MAX_FINDINGS


def test_pipes_escaped_in_cells():
    md = report.render_markdown([_with_findings(1, message="a | b")])
    assert "a \\| b" in md


def test_only_the_first_line_of_a_multiline_message_is_shown():
    # eslint's react-hooks rules embed paragraphs + a code frame in `message`.
    essay = (
        "Calling setState in an effect is bad\n\nLong explanation\n  120 | code frame"
    )
    frag = _with_findings(1, message=essay)
    md = report.render_markdown([frag])
    assert "Calling setState in an effect is bad" in md
    assert "code frame" not in md
    assert report.console_findings(frag)[0].count("\n") == 0


def test_long_messages_truncated():
    frag = _with_findings(1, message="x" * 400)
    md = report.render_markdown([frag])
    assert "…" in md
    assert "x" * 400 not in md


def test_findings_round_trip_through_the_fragment_file(tmp_path):
    ctx = _ctx(tmp_path)
    report.write_fragment(ctx, _with_findings(3))
    loaded = report.load_fragments(ctx.report_dir)[0]
    assert len(loaded.findings) == 3
    assert loaded.findings[0].rule == "no-undef"
    assert loaded.findings[0].location == "src/file0.js:0"


def test_console_findings_are_capped_and_state_remainder():
    lines = report.console_findings(_with_findings(25), limit=5)
    assert len(lines) == 6
    assert lines[-1].strip() == "… and 20 more"
