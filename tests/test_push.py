from redux_build import docker as dockermod
from redux_build.context import RunContext
from redux_build.engines import base as basemod
from redux_build.engines.base import Engine
from redux_build.engines.uv import UvEngine
from redux_build.models import Status
from redux_build.runner import CmdResult

CONFIG = {
    "artifact": {"image": "ghcr.io/reduxisu/quantumsolver"},
    "push": {"on-branch": "main"},
}
SHA = "0123456789abcdef0123456789abcdef01234567"
DIGEST = "sha256:" + "ab" * 32
CI_ENV = {
    "GITHUB_TOKEN": "tok",
    "GITHUB_SHA": SHA,
    "GITHUB_REF_NAME": "main",
    "GITHUB_ACTOR": "ryan",
}


def _ctx(tmp_path, is_github=True, **env):
    return RunContext(cwd=tmp_path, is_github=is_github, env={**CI_ENV, **env})


def _recorder(seen, rc=0, out="", only=None):
    """Records argv; `only` names the docker subcommand that gets `rc`, the rest succeed."""

    def _run(cmd, *_args, **kwargs):
        seen.append((cmd, kwargs))
        code = rc if only in (None, cmd[1]) else 0
        return CmdResult(rc=code, out=out, duration_s=0.1)

    return _run


def _wire(monkeypatch, seen, login_rc=0, push_rc=0, out=""):
    monkeypatch.setattr(dockermod, "run", _recorder(seen, rc=login_rc))
    monkeypatch.setattr(
        basemod, "run", _recorder(seen, rc=push_rc, out=out, only="push")
    )


def _argv(seen):
    return [cmd for cmd, _ in seen]


def test_registry_is_the_host_segment():
    assert dockermod.registry("ghcr.io/reduxisu/quantumsolver") == "ghcr.io"
    assert dockermod.registry("localhost:5000/demo") == "localhost:5000"
    # A Docker Hub short name has no host — logging in to "owner" would be wrong.
    assert dockermod.registry("owner/name") == ""


def test_base_engine_still_skips(tmp_path):
    # test_scaffold asserts every operation on a bare Engine skips; keep that true.
    assert Engine({}).push(_ctx(tmp_path)).status == Status.skipped


def test_skips_without_push_block_before_reading_env(tmp_path, monkeypatch):
    monkeypatch.setattr(basemod, "run", lambda *a, **k: 1 / 0)
    for config in ({}, {"artifact": CONFIG["artifact"]}, {"push": CONFIG["push"]}):
        fragment = UvEngine(config).push(_ctx(tmp_path))
        assert fragment.status == Status.skipped
        assert fragment.summary == "no [push] block"


def test_skips_outside_ci(tmp_path):
    fragment = UvEngine(CONFIG).push(_ctx(tmp_path, is_github=False))
    assert fragment.status == Status.skipped
    assert fragment.summary == "only in CI"


def test_skips_off_the_release_branch(tmp_path):
    # On a pull_request event GITHUB_REF_NAME is `NN/merge`, never the branch itself.
    fragment = UvEngine(CONFIG).push(_ctx(tmp_path, GITHUB_REF_NAME="18/merge"))
    assert fragment.status == Status.skipped
    assert fragment.summary == "only on push to `main`"


def test_fails_without_credentials(tmp_path):
    fragment = UvEngine(CONFIG).push(_ctx(tmp_path, GITHUB_TOKEN="", GITHUB_SHA=""))
    assert fragment.status == Status.failure
    assert fragment.summary == "missing GITHUB_TOKEN, GITHUB_SHA"


def test_logs_in_over_stdin_never_argv(tmp_path, monkeypatch):
    seen = []
    _wire(monkeypatch, seen)
    UvEngine(CONFIG).push(_ctx(tmp_path))

    login, kwargs = seen[0]
    assert login == ["docker", "login", "ghcr.io", "-u", "ryan", "--password-stdin"]
    assert kwargs["input"] == "tok"
    assert not any("tok" in arg for cmd in _argv(seen) for arg in cmd)


def test_tags_then_pushes_sha_before_latest(tmp_path, monkeypatch):
    seen = []
    _wire(monkeypatch, seen)
    fragment = UvEngine(CONFIG).push(_ctx(tmp_path))

    local = "local/quantumsolver:ci"
    sha_ref = "ghcr.io/reduxisu/quantumsolver:0123456"
    assert _argv(seen)[1:] == [
        ["docker", "tag", local, sha_ref],
        ["docker", "push", sha_ref],
        ["docker", "tag", local, "ghcr.io/reduxisu/quantumsolver:latest"],
        ["docker", "push", "ghcr.io/reduxisu/quantumsolver:latest"],
    ]
    assert fragment.status == Status.success
    assert fragment.summary == f"pushed `{sha_ref}`, `:latest`"


def test_reports_the_digest(tmp_path, monkeypatch):
    seen = []
    _wire(monkeypatch, seen, out=f"latest: digest: {DIGEST} size: 1234\n")
    fragment = UvEngine(CONFIG).push(_ctx(tmp_path))
    assert fragment.metrics == {"digest": DIGEST}


def test_dry_run_tags_but_never_pushes(tmp_path, monkeypatch):
    seen = []
    _wire(monkeypatch, seen)
    # Off-branch and outside CI: a dry run is precisely for rehearsing from such places.
    ctx = RunContext(
        cwd=tmp_path,
        is_github=False,
        env={**CI_ENV, "GITHUB_REF_NAME": "feature/18"},
        dry_run=True,
    )
    fragment = UvEngine(CONFIG).push(ctx)

    assert [cmd[1] for cmd in _argv(seen)] == ["login", "tag", "tag"]
    assert fragment.status == Status.warning
    assert fragment.summary == (
        "dry run — logged in, would push `ghcr.io/reduxisu/quantumsolver:0123456`, `:latest`"
    )
    assert fragment.metrics == {}


def test_dry_run_still_needs_credentials(tmp_path):
    ctx = RunContext(cwd=tmp_path, is_github=False, env={}, dry_run=True)
    fragment = UvEngine(CONFIG).push(ctx)
    assert fragment.status == Status.failure
    assert fragment.summary == "missing GITHUB_TOKEN, GITHUB_SHA"


def test_login_failure_pushes_nothing(tmp_path, monkeypatch):
    seen = []
    _wire(monkeypatch, seen, login_rc=1)
    fragment = UvEngine(CONFIG).push(_ctx(tmp_path))
    assert fragment.status == Status.failure
    assert fragment.summary == "login to ghcr.io failed"
    assert len(seen) == 1


def test_push_failure_stops_at_the_first_ref(tmp_path, monkeypatch):
    seen = []
    _wire(monkeypatch, seen, push_rc=1)
    fragment = UvEngine(CONFIG).push(_ctx(tmp_path))
    assert fragment.status == Status.failure
    assert fragment.summary == "docker push failed"
    # login, tag, push — and nothing after the failed push.
    assert [cmd[1] for cmd in _argv(seen)] == ["login", "tag", "push"]
