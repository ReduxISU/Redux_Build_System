from __future__ import annotations

import socket
from pathlib import Path

from redux_build.context import RunContext
from redux_build.runner import run

# Stamped on every container and network rbs creates, so an interrupted run can always be swept:
#   docker ps -aq --filter label=rbs.run | xargs -r docker rm -f
LABEL = "rbs.run"


def local_tag(config: dict) -> str:
    """Tag for the in-run image: built, tested, and only then retagged for the registry."""
    return f"local/{artifact_name(config)}:ci"


def artifact_name(config: dict) -> str:
    artifact = config.get("artifact", {})
    return (
        artifact.get("name")
        or _basename(artifact.get("image"))
        or config.get("package")
        or "app"
    )


def _basename(image: str | None) -> str | None:
    return image.rsplit("/", 1)[-1].split(":")[0] if image else None


def image_size(ctx: RunContext, tag: str) -> str:
    result = run(["docker", "images", tag, "--format", "{{.Size}}"], ctx.cwd)
    lines = result.out.strip().splitlines()
    return lines[0].strip() if result.ok and lines else ""


def network_create(ctx: RunContext, name: str) -> bool:
    result = run(
        ["docker", "network", "create", "--label", f"{LABEL}={name}", name], ctx.cwd
    )
    return result.ok


def network_remove(ctx: RunContext, name: str) -> None:
    run(["docker", "network", "rm", name], ctx.cwd)


def network_connect(ctx: RunContext, network: str, container: str) -> bool:
    result = run(["docker", "network", "connect", network, container], ctx.cwd)
    return result.ok


def network_disconnect(ctx: RunContext, network: str, container: str) -> None:
    run(["docker", "network", "disconnect", network, container], ctx.cwd)


def run_detached(
    ctx: RunContext,
    image: str,
    name: str,
    network: str,
    alias: str,
    env: dict,
    publish: int | None = None,
) -> bool:
    cmd = [
        "docker", "run", "-d",
        "--name", name,
        "--label", f"{LABEL}={network}",
        "--network", network,
        "--network-alias", alias,
    ]  # fmt: skip
    for key, value in env.items():
        cmd += ["-e", f"{key}={value}"]
    if publish is not None:
        # Ephemeral host port on loopback: the fixed one is often already taken (a devcontainer
        # publishing the same appPort, a leftover container), and nothing needs it to be stable.
        cmd += ["-p", f"127.0.0.1::{publish}"]
    cmd.append(image)
    return run(cmd, ctx.cwd).ok


def remove(ctx: RunContext, name: str) -> None:
    """Force-remove a container. Tolerant: teardown must never raise over an already-gone name."""
    run(["docker", "rm", "-f", name], ctx.cwd)


def logs(ctx: RunContext, name: str, tail: int = 20) -> list[str]:
    result = run(["docker", "logs", "--tail", str(tail), name], ctx.cwd)
    return [line for line in result.out.splitlines() if line.strip()]


def is_running(ctx: RunContext, name: str) -> bool:
    result = run(["docker", "inspect", "-f", "{{.State.Running}}", name], ctx.cwd)
    return result.ok and result.out.strip() == "true"


def published_port(ctx: RunContext, name: str, port: int) -> str:
    """Host port docker assigned to `port`, or "" if it was not published."""
    result = run(["docker", "port", name, str(port)], ctx.cwd)
    lines = result.out.strip().splitlines()
    return lines[0].rsplit(":", 1)[-1].strip() if result.ok and lines else ""


def self_id(ctx: RunContext) -> str:
    """This process's own container id, or "" when not running in one.

    `integration-test` uses this to join the ephemeral network from the inside. In a devcontainer
    (and under devcontainers/ci) the docker socket belongs to the host, so containers rbs starts
    are siblings: their published ports do not appear on our loopback and their names do not
    resolve — unless we join their network too.
    """
    if not Path("/.dockerenv").exists():
        return ""
    # Docker sets the container's hostname to its short id unless the image or devcontainer
    # overrides it, so `inspect` doubles as the check that this name really is a container.
    hostname = socket.gethostname().strip()
    result = run(["docker", "inspect", "-f", "{{.Id}}", hostname], ctx.cwd)
    return hostname if result.ok else ""
