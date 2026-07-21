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

    @property
    def ok(self) -> bool:
        return self.rc == 0


def run(
    cmd: list[str] | str,
    cwd: Path,
    env: dict[str, str] | None = None,
    shell: bool = False,
) -> CmdResult:
    start = time.monotonic()
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        shell=shell,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return CmdResult(
        rc=proc.returncode,
        out=proc.stdout,
        duration_s=round(time.monotonic() - start, 2),
    )
