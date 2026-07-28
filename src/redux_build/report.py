from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import requests

from redux_build.context import RunContext
from redux_build.models import Fragment, Status
from redux_build.registry import ENGINES

MARKER = "<!-- rbs-report -->"

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
            )
        )
    return fragments


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


def emit(ctx: RunContext, fragment: Fragment) -> None:
    """Persist the fragment; in CI also append the step summary and step outputs."""
    write_fragment(ctx, fragment)
    line = summary_line(fragment)
    print(line)
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
    return "\n".join([*head, *rows, *tail])


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
    """Render the aggregated table at the bottom of the Actions run, not just in the PR comment."""
    if not ctx.step_summary_path:
        return
    with ctx.step_summary_path.open("a") as handle:
        handle.write(body + "\n")


def has_failure(fragments: list[Fragment]) -> bool:
    return any(f.status == Status.failure for f in fragments)


def post_comment(body: str, ctx: RunContext) -> str:
    token = ctx.env.get("GITHUB_TOKEN")
    event_path = ctx.env.get("GITHUB_EVENT_PATH")
    repo = ctx.env.get("GITHUB_REPOSITORY")
    if not (token and event_path and repo):
        return "not a GitHub PR context — comment skipped"
    event = json.loads(Path(event_path).read_text())
    number = event.get("pull_request", {}).get("number")
    if not number:
        return "not a pull_request event — comment skipped"
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
