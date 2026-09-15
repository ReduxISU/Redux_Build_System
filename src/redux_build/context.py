from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RunContext:
    cwd: Path
    is_github: bool
    variant: str = ""
    env: dict[str, str] = field(default_factory=dict)
    # Rehearse `push` without touching the registry: the CI and branch gates are bypassed so a
    # feature branch can exercise login + tag end to end, and the `docker push` itself is skipped.
    dry_run: bool = False

    @classmethod
    def detect(
        cls, cwd: Path | None = None, variant: str = "", dry_run: bool = False
    ) -> RunContext:
        env = dict(os.environ)
        return cls(
            cwd=cwd or Path.cwd(),
            is_github=env.get("GITHUB_ACTIONS") == "true",
            variant=variant,
            env=env,
            dry_run=dry_run,
        )

    @property
    def step_summary_path(self) -> Path | None:
        value = self.env.get("GITHUB_STEP_SUMMARY")
        return Path(value) if value else None

    @property
    def output_path(self) -> Path | None:
        value = self.env.get("GITHUB_OUTPUT")
        return Path(value) if value else None

    @property
    def sha(self) -> str:
        return self.env.get("GITHUB_SHA", "")

    @property
    def ref_name(self) -> str:
        """Branch (or `NN/merge` on a pull request) — how `push` knows it is on the release branch."""
        return self.env.get("GITHUB_REF_NAME", "")

    @property
    def actor(self) -> str:
        # GHCR authenticates on the token alone, so the username only needs to be non-empty.
        return self.env.get("GITHUB_ACTOR") or "github-actions"

    @property
    def report_dir(self) -> Path:
        value = self.env.get("RBS_REPORT_DIR")
        return Path(value) if value else self.cwd / ".rbs" / "report"
