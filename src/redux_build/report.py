from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import requests

from redux_build.context import RunContext
from redux_build.models import Finding, Fragment, Status
from redux_build.registry import ENGINES

MARKER = "<!-- rbs-report -->"

# Findings shown per operation before truncating. A PR comment is capped at 65536 chars, and an
# unreadable wall of text helps nobody — but the count of what was dropped is always stated.
MAX_FINDINGS = 20
MAX_MESSAGE = 140

_ICON = {
    Status.success: "✅",
    Status.failure: "❌",
    Status.skipped: "⏭️",
    Status.warning: "⚠️",
    Status.blocked: "⛔",
}


def fragment_path(ctx: RunContext, fragment: Fragment) -> Path:
    suffix = f"-{fragment.variant}" if fragment.variant else ""
    return ctx.report_dir / f"{fragment.operation}{suffix}.json"


def write_fragment(ctx: RunContext, fragment: Fragment) -> Path:
    ctx.report_dir.mkdir(parents=True, exist_ok=True)
    path = fragment_path(ctx, fragment)
    path.write_text(json.dumps(fragment.to_dict(), indent=2))
    return path


def load_fragments(report_dir: Path) -> list[Fragment]:
    fragments = []
    for path in sorted(report_dir.glob("*.json")):
        data = json.loads(path.read_text())
        fragments.append(
            Fragment(
                engine=data["engine"],
                operation=data["operation"],
                status=Status(data["status"]),
                summary=data.get("summary", ""),
                variant=data.get("variant", ""),
                metrics=data.get("metrics", {}),
                duration_s=data.get("duration_s", 0.0),
                findings=[
                    Finding(
                        message=item.get("message", ""),
                        location=item.get("location", ""),
                        rule=item.get("rule", ""),
                        severity=item.get("severity", ""),
                    )
                    for item in data.get("findings", [])
                ],
            )
        )
    return fragments


def clear_fragments(report_dir: Path) -> None:
    """Drop fragments from an earlier run so a full pipeline never reports stale results."""
    for path in report_dir.glob("*.json"):
        path.unlink()


def find_blocker(report_dir: Path, required: list[str]) -> str | None:
    """First prerequisite that failed or was itself blocked, if any.

    Read from the fragments already on disk, so `rbs integration-test` short-circuits the same
    way locally as in CI — the dependency policy lives here, never in workflow YAML.
    """
    if not required:
        return None
    by_operation = {f.operation: f.status for f in load_fragments(report_dir)}
    blocking = (Status.failure, Status.blocked)
    return next((name for name in required if by_operation.get(name) in blocking), None)


def summary_line(fragment: Fragment) -> str:
    label = fragment.operation
    if fragment.variant:
        label += f" ({fragment.variant})"
    return f"{_ICON[fragment.status]} {label} — {fragment.summary}"


def console_findings(fragment: Fragment, limit: int = MAX_FINDINGS) -> list[str]:
    """Indented finding lines for the terminal / CI log — one line each, never a wall of text."""
    lines = [
        "    "
        + " ".join(
            part
            for part in (f.severity, f.location, f.rule, _one_line(f.message))
            if part
        )
        for f in fragment.findings[:limit]
    ]
    remaining = len(fragment.findings) - limit
    if remaining > 0:
        lines.append(f"    … and {remaining} more")
    return lines


def emit(ctx: RunContext, fragment: Fragment) -> None:
    """Persist the fragment; in CI also append the step summary and step outputs."""
    write_fragment(ctx, fragment)
    line = summary_line(fragment)
    print(line)
    for finding_line in console_findings(fragment):
        print(finding_line)
    if ctx.step_summary_path:
        with ctx.step_summary_path.open("a") as handle:
            handle.write(line + "\n")
    if ctx.output_path:
        with ctx.output_path.open("a") as handle:
            handle.write(f"status={fragment.status.value}\n")


def render_markdown(fragments: list[Fragment], ctx: RunContext | None = None) -> str:
    tally = Counter(f.status for f in fragments)
    rows = []
    for fragment in sorted(fragments, key=_sort_key(_pipeline_order(fragments))):
        label = fragment.operation + (
            f" · {fragment.variant}" if fragment.variant else ""
        )
        rows.append(
            f"| {label} | {_ICON[fragment.status]} | {fragment.summary} "
            f"| {_duration(fragment)} |"
        )
    overall = "❌" if tally[Status.failure] else "✅"
    head = [MARKER, "## Redux Build System — CI Report"]
    subtitle = _subtitle(fragments, ctx)
    if subtitle:
        head.append(subtitle)
    head += ["", "| Operation | Status | Summary | Time |", "|---|:--:|---|--:|"]
    tail = ["", f"**Overall: {overall} {_tally_line(tally)}**"]
    details = [
        block
        for fragment in sorted(fragments, key=_sort_key(_pipeline_order(fragments)))
        if (block := _details_block(fragment))
    ]
    return "\n".join([*head, *rows, *tail, *details])


def _details_block(fragment: Fragment) -> str:
    """A collapsed list of what actually failed — the *which* behind the summary count."""
    if not fragment.findings:
        return ""
    label = fragment.operation + (f" · {fragment.variant}" if fragment.variant else "")
    rows = [
        "| "
        + " | ".join(
            _cell(value)
            for value in (
                finding.severity,
                finding.location,
                finding.rule,
                finding.message,
            )
        )
        + " |"
        for finding in fragment.findings[:MAX_FINDINGS]
    ]
    remaining = len(fragment.findings) - MAX_FINDINGS
    if remaining > 0:
        rows.append(f"| | | | _… and {remaining} more_ |")
    return "\n".join(
        [
            "",
            f"<details><summary>{_ICON[fragment.status]} {label} — "
            f"{fragment.summary}</summary>",
            "",
            "| Severity | Location | Rule | Message |",
            "|---|---|---|---|",
            *rows,
            "",
            "</details>",
        ]
    )


def _one_line(value: str, limit: int = MAX_MESSAGE) -> str:
    """First line only, whitespace-collapsed and length-capped.

    Some tools return essays: eslint's `react-hooks` rules embed several paragraphs plus a code
    frame in `message`. The full text stays in the fragment JSON; only the display is trimmed.
    """
    lines = [line.strip() for line in str(value).splitlines() if line.strip()]
    text = " ".join(lines[0].split()) if lines else ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _cell(value: str) -> str:
    """Markdown table cells cannot contain a raw pipe or newline."""
    return _one_line(value).replace("|", "\\|")


def _pipeline_order(fragments: list[Fragment]) -> list[str]:
    """The producing toolchain's operation order, so rows read in execution order."""
    engines = {f.engine for f in fragments if f.engine}
    if len(engines) != 1:
        return []
    engine_cls = ENGINES.get(engines.pop())
    return engine_cls.order if engine_cls else []


def _sort_key(order: list[str]):
    def key(fragment: Fragment) -> tuple:
        position = (
            order.index(fragment.operation)
            if fragment.operation in order
            else len(order)
        )
        return (position, fragment.operation, fragment.variant)

    return key


def _duration(fragment: Fragment) -> str:
    return f"{fragment.duration_s:.1f}s" if fragment.duration_s else "—"


def _tally_line(tally: Counter) -> str:
    passed = tally[Status.success] + tally[Status.warning]
    parts = [f"{passed} passed", f"{tally[Status.failure]} failed"]
    parts += [f"{tally[Status.skipped]} skipped"]
    if tally[Status.blocked]:
        parts.append(f"{tally[Status.blocked]} blocked")
    return " · ".join(parts)


def _subtitle(fragments: list[Fragment], ctx: RunContext | None) -> str:
    engines = sorted({f.engine for f in fragments if f.engine})
    parts = []
    if engines:
        parts.append("`" + "`, `".join(engines) + "`")
    sha = ctx.env.get("GITHUB_SHA") if ctx else None
    if sha:
        parts.append(f"commit `{sha[:7]}`")
    return " · ".join(parts)


def write_step_summary(ctx: RunContext, body: str) -> None:
    """Render the aggregated table at the bottom of the Actions run, not just in the PR comment.

    Best-effort: inside a devcontainer the forwarded runner path may not exist, and losing the
    summary must never fail the pipeline — the workflow also appends `report.md` on the runner.
    """
    if not ctx.step_summary_path:
        return
    try:
        with ctx.step_summary_path.open("a") as handle:
            handle.write(body + "\n")
    except OSError as exc:
        print(f"rbs: step summary unavailable ({exc})")


def has_failure(fragments: list[Fragment]) -> bool:
    return any(f.status == Status.failure for f in fragments)


def pull_request_number(ctx: RunContext) -> int | None:
    """The PR to comment on.

    `RBS_PR_NUMBER` takes precedence because `GITHUB_EVENT_PATH` points at a runner temp file
    that is not mounted inside a devcontainer — so when rbs runs via `devcontainers/ci`, the
    workflow passes the number in explicitly.
    """
    explicit = (ctx.env.get("RBS_PR_NUMBER") or "").strip()
    if explicit.isdigit():
        return int(explicit)
    event_path = ctx.env.get("GITHUB_EVENT_PATH")
    if not event_path or not Path(event_path).is_file():
        return None
    event = json.loads(Path(event_path).read_text())
    return event.get("pull_request", {}).get("number")


def post_comment(body: str, ctx: RunContext) -> str:
    token = ctx.env.get("GITHUB_TOKEN")
    repo = ctx.env.get("GITHUB_REPOSITORY")
    if not (token and repo):
        return "not a GitHub context — comment skipped"
    number = pull_request_number(ctx)
    if not number:
        return "not a pull request — comment skipped"
    api = ctx.env.get("GITHUB_API_URL", "https://api.github.com")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    issues = f"{api}/repos/{repo}/issues"
    try:
        existing = _find_marker_comment(f"{issues}/{number}/comments", headers)
        if existing:
            resp = requests.patch(
                f"{issues}/comments/{existing}",
                headers=headers,
                json={"body": body},
                timeout=30,
            )
            resp.raise_for_status()
            return f"updated comment {existing}"
        resp = requests.post(
            f"{issues}/{number}/comments",
            headers=headers,
            json={"body": body},
            timeout=30,
        )
        resp.raise_for_status()
        return "created comment"
    except requests.RequestException as exc:
        return f"comment failed: {exc}"


def _find_marker_comment(list_url: str, headers: dict) -> int | None:
    page = 1
    while True:
        resp = requests.get(
            list_url,
            headers=headers,
            params={"per_page": 100, "page": page},
            timeout=30,
        )
        resp.raise_for_status()
        comments = resp.json()
        if not comments:
            return None
        for comment in comments:
            if MARKER in comment.get("body", ""):
                return comment["id"]
        page += 1
