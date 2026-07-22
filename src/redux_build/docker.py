from __future__ import annotations

from redux_build.context import RunContext
from redux_build.runner import run


def local_tag(config: dict) -> str:
    """Tag for the in-run image: built, tested, and only then retagged for the registry."""
    artifact = config.get("artifact", {})
    name = (
        artifact.get("name")
        or _basename(artifact.get("image"))
        or config.get("package")
        or "app"
    )
    return f"local/{name}:ci"


def _basename(image: str | None) -> str | None:
    return image.rsplit("/", 1)[-1].split(":")[0] if image else None


def image_size(ctx: RunContext, tag: str) -> str:
    result = run(["docker", "images", tag, "--format", "{{.Size}}"], ctx.cwd)
    lines = result.out.strip().splitlines()
    return lines[0].strip() if result.ok and lines else ""
