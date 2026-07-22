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


def test_build_failure(tmp_path, monkeypatch):
    (tmp_path / "Dockerfile").write_text("FROM alpine\n")
    monkeypatch.setattr(basemod, "run", _fixed(1, "error"))
    frag = UvEngine({"package": "demo"}).build(_ctx(tmp_path))
    assert frag.status == Status.failure
    assert frag.summary == "docker build failed"
