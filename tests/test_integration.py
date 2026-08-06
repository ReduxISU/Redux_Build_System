import pytest

from redux_build import docker as dockermod
from redux_build import stack as stackmod
from redux_build.context import RunContext
from redux_build.engines import base as basemod
from redux_build.engines.base import Engine
from redux_build.engines.npm import NpmEngine
from redux_build.models import Status
from redux_build.runner import CmdResult

CONFIG = {
    "artifact": {
        "image": "ghcr.io/reduxisu/redux_gui",
        "port": 3000,
        "health-path": "/api/health",
    },
    "integration": {
        "command": "npm run test:e2e",
        "timeout": 30,
        "env": {"REDUX_BASE_URL": "http://redux-api:27000/"},
        "services": [
            {
                "name": "redux-api",
                "image": "ghcr.io/reduxisu/redux:latest",
                "port": 27000,
                "health-path": "/Navigation/Batch/allProblems",
            }
        ],
    },
}


def _ctx(tmp_path):
    return RunContext(cwd=tmp_path, is_github=False, env={"PATH": "/usr/bin"})


def _docker(seen, running="true", logs="startup failed"):
    """Stand-in for every `docker` invocation; records the argv it was given."""

    def _run(cmd, *_args, **_kwargs):
        seen.append(cmd)
        joined = " ".join(cmd)
        if "{{.State.Running}}" in joined:
            return CmdResult(rc=0, out=running, duration_s=0.0)
        if cmd[:2] == ["docker", "port"]:
            return CmdResult(rc=0, out="127.0.0.1:49999", duration_s=0.0)
        if cmd[:2] == ["docker", "logs"]:
            return CmdResult(rc=0, out=logs, duration_s=0.0)
        return CmdResult(rc=0, out="", duration_s=0.0)

    return _run


def _wire(monkeypatch, seen, *, attached="rbs-self", ready=True, running="true"):
    monkeypatch.setattr(dockermod, "run", _docker(seen, running=running))
    monkeypatch.setattr(dockermod, "self_id", lambda ctx: attached)
    monkeypatch.setattr(stackmod, "probe", lambda url: 200 if ready else 0)


def _commands(seen, *prefix):
    return [cmd for cmd in seen if cmd[: len(prefix)] == list(prefix)]


def test_skips_without_an_integration_command(tmp_path):
    fragment = NpmEngine({"artifact": {"port": 3000}}).integration_test(_ctx(tmp_path))
    assert fragment.status == Status.skipped
    assert "[integration]" in fragment.summary


def test_base_engine_still_skips(tmp_path):
    # test_scaffold asserts every operation on a bare Engine skips; keep that true.
    assert Engine({}).integration_test(_ctx(tmp_path)).status == Status.skipped


def test_starts_dependencies_before_the_artifact(tmp_path, monkeypatch):
    seen = []
    _wire(monkeypatch, seen)
    monkeypatch.setattr(basemod, "run", lambda *a, **k: CmdResult(0, "4 passed", 0.1))

    NpmEngine(CONFIG).integration_test(_ctx(tmp_path))

    started = _commands(seen, "docker", "run", "-d")
    assert [cmd[-1] for cmd in started] == [
        "ghcr.io/reduxisu/redux:latest",
        "local/redux_gui:ci",
    ]
    # The artifact is configured to reach the backend by alias, so the alias has to be the
    # declared service name and the dependency has to exist first.
    assert "--network-alias" in started[0] and "redux-api" in started[0]
    assert "REDUX_BASE_URL=http://redux-api:27000/" in started[1]


def test_hands_the_stack_addresses_to_the_suite(tmp_path, monkeypatch):
    seen = []
    _wire(monkeypatch, seen)
    captured = {}

    def _run(cmd, _cwd, env=None, shell=False, **_kwargs):
        captured.update(cmd=cmd, env=env, shell=shell)
        return CmdResult(0, "4 passed", 0.1)

    monkeypatch.setattr(basemod, "run", _run)
    NpmEngine(CONFIG).integration_test(_ctx(tmp_path))

    assert captured["cmd"] == "npm run test:e2e"
    assert captured["shell"] is True
    assert captured["env"]["RBS_BASE_URL"] == "http://redux_gui:3000"
    assert captured["env"]["RBS_URL_REDUX_API"] == "http://redux-api:27000"
    # The command still needs the ambient environment (PATH, and whatever CI injected).
    assert captured["env"]["PATH"] == "/usr/bin"


def test_reports_the_test_counts(tmp_path, monkeypatch):
    seen = []
    _wire(monkeypatch, seen)
    monkeypatch.setattr(basemod, "run", lambda *a, **k: CmdResult(0, "4 passed", 0.1))

    fragment = NpmEngine(CONFIG).integration_test(_ctx(tmp_path))
    assert fragment.status == Status.success
    assert fragment.summary == "/api/health ready · 4 passed"


def test_failing_suite_fails_the_operation(tmp_path, monkeypatch):
    seen = []
    _wire(monkeypatch, seen)
    monkeypatch.setattr(
        basemod, "run", lambda *a, **k: CmdResult(1, "1 failed\n3 passed", 0.1)
    )

    fragment = NpmEngine(CONFIG).integration_test(_ctx(tmp_path))
    assert fragment.status == Status.failure
    assert fragment.summary == "1 failed · 3 passed"


def test_service_that_never_answers_fails_with_its_logs(tmp_path, monkeypatch):
    seen = []
    _wire(monkeypatch, seen, ready=False)
    monkeypatch.setattr(
        basemod, "run", lambda *a, **k: pytest.fail("suite must not run")
    )
    config = {**CONFIG, "integration": {**CONFIG["integration"], "timeout": 0}}

    fragment = NpmEngine(config).integration_test(_ctx(tmp_path))

    assert fragment.status == Status.failure
    assert fragment.summary == "`redux-api` not ready after 0s"
    # Without the logs, "not ready" tells you nothing about why.
    assert [finding.message for finding in fragment.findings] == ["startup failed"]


def test_exited_container_fails_immediately(tmp_path, monkeypatch):
    # A dead container cannot become ready, so waiting out the 30s timeout would be pure delay.
    seen = []
    _wire(monkeypatch, seen, ready=False, running="false")
    monkeypatch.setattr(
        stackmod.time,
        "sleep",
        lambda _s: pytest.fail("must not wait on a dead container"),
    )

    fragment = NpmEngine(CONFIG).integration_test(_ctx(tmp_path))
    assert fragment.status == Status.failure
    # Not "not ready after 30s" — it died in a second and saying otherwise misleads the reader.
    assert fragment.summary == "`redux-api` exited before becoming ready"


def test_tears_down_even_when_the_suite_raises(tmp_path, monkeypatch):
    seen = []
    _wire(monkeypatch, seen)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("runner exploded")

    monkeypatch.setattr(basemod, "run", _boom)
    with pytest.raises(RuntimeError):
        NpmEngine(CONFIG).integration_test(_ctx(tmp_path))

    # Both containers and the network go, even on the way out through an exception — there is no
    # other cleanup hook, so anything missed here leaks for good.
    removed = [cmd[-1] for cmd in _commands(seen, "docker", "rm", "-f")]
    assert [name.rsplit("-", 1)[-1] for name in removed] == ["api", "redux_gui"]
    assert len(removed) == 2
    assert _commands(seen, "docker", "network", "rm")


def test_joins_the_network_when_rbs_is_itself_a_container(tmp_path, monkeypatch):
    seen = []
    _wire(monkeypatch, seen, attached="rbs-self")
    monkeypatch.setattr(basemod, "run", lambda *a, **k: CmdResult(0, "", 0.1))

    NpmEngine(CONFIG).integration_test(_ctx(tmp_path))

    assert _commands(seen, "docker", "network", "connect")
    assert _commands(seen, "docker", "network", "disconnect")
    # Joined the network, so nothing needs publishing to the host.
    assert not any("-p" in cmd for cmd in _commands(seen, "docker", "run", "-d"))


def test_publishes_ports_when_rbs_runs_on_the_host(tmp_path, monkeypatch):
    seen = []
    _wire(monkeypatch, seen, attached="")
    captured = {}

    def _run(cmd, _cwd, env=None, **_kwargs):
        captured.update(env=env)
        return CmdResult(0, "", 0.1)

    monkeypatch.setattr(basemod, "run", _run)
    NpmEngine(CONFIG).integration_test(_ctx(tmp_path))

    started = _commands(seen, "docker", "run", "-d")
    assert ["-p", "127.0.0.1::27000"] == started[0][-3:-1]
    assert not _commands(seen, "docker", "network", "connect")
    assert captured["env"]["RBS_BASE_URL"] == "http://127.0.0.1:49999"


def test_run_id_and_container_names_are_scoped_to_the_artifact(tmp_path, monkeypatch):
    seen = []
    _wire(monkeypatch, seen)
    monkeypatch.setattr(basemod, "run", lambda *a, **k: CmdResult(0, "", 0.1))

    NpmEngine(CONFIG).integration_test(_ctx(tmp_path))

    network = _commands(seen, "docker", "network", "create")[0][-1]
    assert network.startswith("rbs-redux_gui-")
    for cmd in _commands(seen, "docker", "run", "-d"):
        assert cmd[cmd.index("--name") + 1].startswith(network)
