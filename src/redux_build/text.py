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
    return " · ".join(part for part in (failed, passed) if part)


def relative(path: str, cwd: Path) -> str:
    """Repo-relative form of a tool-reported path, unchanged when it lies outside the repo."""
    try:
        return str(Path(path).relative_to(cwd))
    except ValueError:
        return path
