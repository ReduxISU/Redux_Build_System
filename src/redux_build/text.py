from __future__ import annotations

import re
from pathlib import Path


def search(pattern: str, text: str, default: str) -> str:
    """First match of `pattern` in `text`, else `default` — builds one-line Fragment summaries."""
    match = re.search(pattern, text)
    return match.group(0) if match else default


def relative(path: str, cwd: Path) -> str:
    """Repo-relative form of a tool-reported path, unchanged when it lies outside the repo."""
    try:
        return str(Path(path).relative_to(cwd))
    except ValueError:
        return path
