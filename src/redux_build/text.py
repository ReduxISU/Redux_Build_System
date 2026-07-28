from __future__ import annotations

import re


def search(pattern: str, text: str, default: str) -> str:
    """First match of `pattern` in `text`, else `default` — builds one-line Fragment summaries."""
    match = re.search(pattern, text)
    return match.group(0) if match else default
