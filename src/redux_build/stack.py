"""The ephemeral container topology `integration-test` runs against.

One network, one container per declared service, plus the image `build` just produced. Everything
is torn down afterwards, always.

Addressing is the subtle part. rbs usually runs inside a devcontainer wired for
docker-outside-of-docker, so the containers it starts are *siblings on the host*, not children:
their published ports are not on our loopback and their names do not resolve. So we join the
network ourselves and address everything by network alias, which then reads identically on a
laptop, in a devcontainer, and under devcontainers/ci. Only when rbs is running directly on the
host (no container to join) do we fall back to publishing ports and talking to 127.0.0.1.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from urllib import error, request

from redux_build import docker
from redux_build.context import RunContext

POLL_INTERVAL_S = 0.5
DEFAULT_TIMEOUT_S = 180


@dataclass
class Service:
    name: str
    image: str
    port: int
    health_path: str
    env: dict = field(default_factory=dict)


@dataclass
class Stack:
    network: str
    containers: list[str] = field(default_factory=list)
    urls: dict[str, str] = field(default_factory=dict)
    attached: str = ""


def plan(config: dict) -> list[Service]:
    """Services to start, dependencies first and the artifact under test last.

    Order matters: the artifact is usually configured to talk to a dependency by alias, so the
    dependency should already be resolvable when it boots.
    """
    integration = config.get("integration", {})
    artifact = config.get("artifact", {})
    services = [
        Service(
            name=declared.get("name", ""),
            image=declared.get("image", ""),
            port=int(declared.get("port", 0)),
            health_path=declared.get("health-path", "/"),
            env=declared.get("env", {}),
        )
        for declared in integration.get("services", [])
    ]
    return services + [
        Service(
            name=docker.artifact_name(config),
            image=docker.local_tag(config),
            port=int(artifact.get("port", 0)),
            health_path=artifact.get("health-path", "/"),
            env=integration.get("env", {}),
        )
    ]


def bring_up(ctx: RunContext, services: list[Service], run_id: str) -> Stack:
    """Start every service. Returns whatever got created, so teardown can clean up a partial run."""
    stack = Stack(network=run_id)
    docker.network_create(ctx, run_id)
    stack.attached = docker.self_id(ctx)
    if stack.attached:
        docker.network_connect(ctx, run_id, stack.attached)
    for service in services:
        container = f"{run_id}-{service.name}"
        started = docker.run_detached(
            ctx,
            image=service.image,
            name=container,
            network=run_id,
            alias=service.name,
            env=service.env,
            publish=None if stack.attached else service.port,
        )
        if not started:
            return stack
        stack.containers.append(container)
        stack.urls[service.name] = _base_url(ctx, stack, service, container)
    return stack


def _base_url(ctx: RunContext, stack: Stack, service: Service, container: str) -> str:
    if stack.attached:
        return f"http://{service.name}:{service.port}"
    return f"http://127.0.0.1:{docker.published_port(ctx, container, service.port)}"


def wait_until_ready(
    ctx: RunContext, stack: Stack, services: list[Service], timeout_s: int
) -> str:
    """Poll every service's health path. Returns "" when all are up, else the one that failed."""
    for service in services:
        container = f"{stack.network}-{service.name}"
        url = stack.urls.get(service.name, "")
        if not url or not _await_service(
            ctx, url + service.health_path, container, timeout_s
        ):
            return service.name
    return ""


def _await_service(ctx: RunContext, url: str, container: str, timeout_s: int) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        # 0 means "could not connect", so the lower bound matters as much as the upper one.
        if 200 <= probe(url) < 400:
            return True
        # A container that has already exited is never going to answer; failing now turns a
        # three-minute wait into an immediate error with the logs attached.
        if not docker.is_running(ctx, container):
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_INTERVAL_S)


def probe(url: str) -> int:
    """HTTP status for `url`, or 0 if it could not be reached at all."""
    try:
        with request.urlopen(
            url, timeout=5
        ) as response:  # noqa: S310 - fixed http scheme
            return response.status
    except error.HTTPError as exc:
        return exc.code
    except (error.URLError, OSError, ValueError):
        return 0


def tear_down(ctx: RunContext, stack: Stack) -> None:
    for container in stack.containers:
        docker.remove(ctx, container)
    if stack.attached:
        docker.network_disconnect(ctx, stack.network, stack.attached)
    docker.network_remove(ctx, stack.network)


def run_id(config: dict) -> str:
    # The pid keeps concurrent runs on one machine (two repos, or a re-run) from colliding on
    # network and container names.
    return f"rbs-{docker.artifact_name(config)}-{os.getpid()}"


def test_env(stack: Stack, artifact: Service) -> dict:
    """Addresses handed to the test command: the artifact as RBS_BASE_URL, each service as
    RBS_URL_<NAME>."""
    env = {"RBS_BASE_URL": stack.urls.get(artifact.name, "")}
    for name, url in stack.urls.items():
        env[f"RBS_URL_{name.upper().replace('-', '_')}"] = url
    return env
