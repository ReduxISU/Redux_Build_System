from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CmdResult:
    rc: int
    out: str
    duration_s: float
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.rc == 0


def run(
    cmd: list[str] | str,
    cwd: Path,
    env: dict[str, str] | None = None,
    shell: bool = False,
    merge_stderr: bool = True,
) -> CmdResult:
    """Run `cmd`, capturing rc, output and wall time.

    `merge_stderr=False` keeps stderr out of `out` — required when parsing a tool's JSON, since
    several (biome, npm) print notices to stderr that would otherwise corrupt the document.
    """
    start = time.monotonic()
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        shell=shell,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
    )
    return CmdResult(
        rc=proc.returncode,
        out=proc.stdout,
        duration_s=round(time.monotonic() - start, 2),
        err=proc.stderr or "",
    )
