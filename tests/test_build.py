from redux_build import docker
from redux_build.context import RunContext
from redux_build.engines import base as basemod
from redux_build.engines.uv import UvEngine
from redux_build.models import Status
from redux_build.runner import CmdResult


def _ctx(tmp_path):
    return RunContext(cwd=tmp_path, is_github=False, env={})


def _fixed(rc, out):
    def _run(*_args, **_kwargs):
        return CmdResult(rc=rc, out=out, duration_s=1.0)

    return _run


def test_local_tag_from_image():
    config = {"artifact": {"image": "ghcr.io/reduxisu/quantumsolver"}}
    assert docker.local_tag(config) == "local/quantumsolver:ci"


def test_local_tag_falls_back_to_package():
    assert docker.local_tag({"package": "redux_build"}) == "local/redux_build:ci"


def test_build_skips_without_dockerfile(tmp_path):
    frag = UvEngine({}).build(_ctx(tmp_path))
    assert frag.status == Status.skipped
    assert "Dockerfile" in frag.summary


def test_build_success_reports_tag_and_size(tmp_path, monkeypatch):
    (tmp_path / "Dockerfile").write_text("FROM alpine\n")
    monkeypatch.setattr(basemod, "run", _fixed(0, "built"))
    monkeypatch.setattr(docker, "image_size", lambda ctx, tag: "12 MB")
    frag = UvEngine({"artifact": {"image": "ghcr.io/x/demo"}}).build(_ctx(tmp_path))
    assert frag.status == Status.success
    assert "local/demo:ci" in frag.summary
    assert "12 MB" in frag.summary


def test_build_in_ci_uses_no_gha_cache(tmp_path, monkeypatch):
    # devcontainers/ci runs the default buildx driver with no ACTIONS_* env, where
    # `type=gha` is a hard error before the build even starts.
    (tmp_path / "Dockerfile").write_text("FROM alpine\n")
    seen = []

    def _run(cmd, *_args, **_kwargs):
        seen.append(cmd)
        return CmdResult(rc=0, out="", duration_s=0.1)

    monkeypatch.setattr(basemod, "run", _run)
    monkeypatch.setattr(docker, "image_size", lambda ctx, tag: "")
    ctx = RunContext(cwd=tmp_path, is_github=True, env={})
    UvEngine({"package": "demo"}).build(ctx)
    assert seen[0][:4] == ["docker", "buildx", "build", "--load"]
    assert not any("type=gha" in arg for arg in seen[0])


def test_build_failure(tmp_path, monkeypatch):
    (tmp_path / "Dockerfile").write_text("FROM alpine\n")
    monkeypatch.setattr(basemod, "run", _fixed(1, "error"))
    frag = UvEngine({"package": "demo"}).build(_ctx(tmp_path))
    assert frag.status == Status.failure
    assert frag.summary == "docker build failed"


HOOK = "dotnet run --project Tools/Stats -- --fill"


def _hooked(tmp_path, monkeypatch, is_github=True, local=None, hook_rc=0):
    """Build with a `[build] pre-command`; returns the fragment and every (cmd, kwargs) run."""
    (tmp_path / "Dockerfile").write_text("FROM alpine\n")
    seen = []

    def _run(cmd, *_args, **kwargs):
        seen.append((cmd, kwargs))
        rc = hook_rc if cmd == HOOK else 0
        return CmdResult(rc=rc, out="", duration_s=1.0)

    monkeypatch.setattr(basemod, "run", _run)
    monkeypatch.setattr(docker, "image_size", lambda ctx, tag: "")
    build = {"pre-command": HOOK}
    if local is not None:
        build["pre-command-local"] = local
    ctx = RunContext(cwd=tmp_path, is_github=is_github, env={"GITHUB_TOKEN": "tok"})
    frag = UvEngine({"package": "demo", "build": build}).build(ctx)
    return frag, seen


def test_build_without_build_block_runs_only_docker(tmp_path, monkeypatch):
    (tmp_path / "Dockerfile").write_text("FROM alpine\n")
    seen = []

    def _run(cmd, *_args, **_kwargs):
        seen.append(cmd)
        return CmdResult(rc=0, out="", duration_s=1.0)

    monkeypatch.setattr(basemod, "run", _run)
    monkeypatch.setattr(docker, "image_size", lambda ctx, tag: "")
    frag = UvEngine({"package": "demo"}).build(_ctx(tmp_path))
    assert len(seen) == 1
    assert "pre-command" not in frag.summary
    assert frag.duration_s == 1.0


def test_pre_command_runs_before_build_in_ci(tmp_path, monkeypatch):
    frag, seen = _hooked(tmp_path, monkeypatch)
    hook, kwargs = seen[0]
    assert hook == HOOK
    # A string from rbs.toml, so it goes through the shell — and it needs the token CI forwards.
    assert kwargs["shell"] is True
    assert kwargs["env"]["GITHUB_TOKEN"] == "tok"
    assert seen[1][0][:3] == ["docker", "buildx", "build"]
    assert frag.status == Status.success
    assert frag.summary.endswith(" · ran pre-command")
    assert frag.duration_s == 2.0


def test_pre_command_failure_fails_build_without_docker(tmp_path, monkeypatch):
    frag, seen = _hooked(tmp_path, monkeypatch, hook_rc=1)
    assert frag.status == Status.failure
    assert frag.summary == "pre-command failed"
    assert frag.duration_s == 1.0
    assert [cmd for cmd, _ in seen] == [HOOK]


def test_pre_command_skipped_outside_ci(tmp_path, monkeypatch):
    # It typically rewrites tracked files; on a laptop that is a dirty tree waiting to be committed.
    frag, seen = _hooked(tmp_path, monkeypatch, is_github=False)
    assert HOOK not in [cmd for cmd, _ in seen]
    assert frag.status == Status.success
    assert frag.summary.endswith(" · pre-command skipped outside CI")


def test_pre_command_local_opts_in_outside_ci(tmp_path, monkeypatch):
    frag, seen = _hooked(tmp_path, monkeypatch, is_github=False, local=True)
    assert seen[0][0] == HOOK
    assert frag.status == Status.success
    assert frag.summary.endswith(" · ran pre-command")


def test_pre_command_not_run_without_dockerfile(tmp_path, monkeypatch):
    monkeypatch.setattr(basemod, "run", lambda *a, **k: 1 / 0)
    ctx = RunContext(cwd=tmp_path, is_github=True, env={})
    frag = UvEngine({"build": {"pre-command": HOOK}}).build(ctx)
    assert frag.status == Status.skipped
    assert "Dockerfile" in frag.summary
