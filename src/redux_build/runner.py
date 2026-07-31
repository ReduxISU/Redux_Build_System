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
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            shell=shell,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
        )
    except (FileNotFoundError, NotADirectoryError):
        # A tool missing from this environment is a failed operation, not a crashed pipeline —
        # every other gate must still get its turn. 127 is the shell's "command not found".
        name = cmd if isinstance(cmd, str) else cmd[0]
        return CmdResult(
            rc=127,
            out=f"rbs: command not found: {name}",
            duration_s=round(time.monotonic() - start, 2),
        )
    return CmdResult(
        rc=proc.returncode,
        out=proc.stdout,
        duration_s=round(time.monotonic() - start, 2),
        err=proc.stderr or "",
    )
