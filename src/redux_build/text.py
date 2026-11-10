from __future__ import annotations

import re
from pathlib import Path


def search(pattern: str, text: str, default: str) -> str:
    """First match of `pattern` in `text`, else `default` — builds one-line Fragment summaries."""
    match = re.search(pattern, text)
    return match.group(0) if match else default


def test_counts(out: str) -> str:
    """`"1 failed · 8 passed"` from a test runner's tail, or "" when it reported neither.

    The generic shape, for runners rbs does not own. `uv` and `npm` keep their own variants
    because they read more than this: coverage totals and `node --test`'s "pass 7" wording.
    """
    passed = search(r"\d+ passed", out, "")
    failed = search(r"\d+ failed", out, "")
    if not (passed or failed):
        # Microsoft Testing Platform words its tail differently (see mtp_counts), so an
        # integration suite run with `dotnet test --project` under MTP still gets its counts.
        counts = mtp_counts(out)
        if counts:
            failed = f"{counts['failed']} failed" if counts["failed"] else ""
            passed = f"{counts['succeeded']} passed" if counts["succeeded"] else ""
    return " · ".join(part for part in (failed, passed) if part)


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_MTP_COUNT = re.compile(r"^\s*(total|failed|succeeded|skipped):\s*(\d+)\s*$", re.M)


def mtp_counts(out: str) -> dict[str, int] | None:
    """Counts from the last `Test run summary:` block Microsoft Testing Platform prints, or None
    when there is none. `dotnet test` prints one such block for the whole run, however many test
    projects it ran:

        Test run summary: Passed!
          total: 2
          failed: 0
          succeeded: 2
          skipped: 0
          duration: 5s 977ms
    """
    text = _ANSI.sub("", out)
    start = text.rfind("Test run summary:")
    if start < 0:
        return None
    counts = dict.fromkeys(("total", "failed", "succeeded", "skipped"), 0)
    for label, value in _MTP_COUNT.findall(text[start:]):
        counts[label] = int(value)
    return counts


def relative(path: str, cwd: Path) -> str:
    """Repo-relative form of a tool-reported path, unchanged when it lies outside the repo."""
    try:
        return str(Path(path).relative_to(cwd))
    except ValueError:
        return path
