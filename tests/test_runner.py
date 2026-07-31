from redux_build.runner import run


def test_missing_binary_is_a_failed_result_not_an_exception(tmp_path):
    # rbs runs in varied environments (a .NET SDK image has no docker, for instance).
    # A missing tool must fail its own operation and let the rest of the pipeline continue.
    result = run(["definitely-not-a-real-binary", "--version"], tmp_path)
    assert not result.ok
    assert result.rc == 127
    assert "command not found" in result.out
    assert "definitely-not-a-real-binary" in result.out


def test_missing_binary_named_for_a_shell_string(tmp_path):
    result = run("definitely-not-a-real-binary", tmp_path, shell=False)
    assert result.rc == 127
    assert "definitely-not-a-real-binary" in result.out


def test_successful_command_still_captures_output(tmp_path):
    result = run(["echo", "hello"], tmp_path)
    assert result.ok
    assert result.out.strip() == "hello"


def test_stderr_kept_separate_when_not_merged(tmp_path):
    result = run(["sh", "-c", "echo out; echo err >&2"], tmp_path, merge_stderr=False)
    assert result.out.strip() == "out"
    assert result.err.strip() == "err"


def test_stderr_merged_by_default(tmp_path):
    result = run(["sh", "-c", "echo out; echo err >&2"], tmp_path)
    assert "err" in result.out
