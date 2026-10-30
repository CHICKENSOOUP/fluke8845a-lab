import ast
import re
import subprocess
import tomllib

import pytest

from scripts.run_tests_sharded import SHARDS, ROOT, validate_shard_coverage


def test_shard_plan_covers_every_python_test_module_once():
    validate_shard_coverage(ROOT)
    modules = [module for shard in SHARDS.values() for module in shard]
    assert len(modules) == len(set(modules))


def test_shard_runner_timeout_is_optional_and_can_exceed_one_minute(monkeypatch):
    import scripts.run_tests_sharded as test_runner

    calls = []
    monkeypatch.setattr(test_runner, "run_shards", lambda **kwargs: calls.append(kwargs) or 0)

    assert test_runner.main([]) == 0
    assert calls[-1]["timeout_seconds"] is None

    assert test_runner.main(["--timeout-seconds", "600"]) == 0
    assert calls[-1]["timeout_seconds"] == 600


def test_gui_end_to_end_subprocess_watchdogs_are_not_subminute():
    for relative_path in ("tests/test_app.py", "tests/test_gui.py"):
        module = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
        timeouts = []
        for node in ast.walk(module):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "run" or not isinstance(node.func.value, ast.Name):
                continue
            if node.func.value.id != "subprocess":
                continue
            timeout = next((keyword.value for keyword in node.keywords if keyword.arg == "timeout"), None)
            if isinstance(timeout, ast.Constant) and isinstance(timeout.value, (int, float)):
                timeouts.append(timeout.value)
        assert timeouts, f"{relative_path} must retain explicit child-process watchdogs"
        assert all(timeout >= 60 for timeout in timeouts), (relative_path, timeouts)


def test_runner_environment_includes_project_src_and_preserves_existing_path(tmp_path):
    import os

    from scripts.run_tests_sharded import runner_environment

    existing = str(tmp_path / "extra-src")
    env = runner_environment(tmp_path, {"PYTHONPATH": existing, "KEEP": "yes"})

    assert env["PYTHONPATH"].split(os.pathsep) == [str(tmp_path / "src"), existing]
    assert env["KEEP"] == "yes"


def test_default_pytest_artifacts_are_in_the_repository_build_directory():
    import scripts.run_tests_sharded as test_runner

    assert test_runner.default_output_root() == ROOT / "build" / "test-artifacts" / "pytest-shards"


def test_shard_elapsed_time_stops_when_each_process_finishes(tmp_path, monkeypatch, capsys):
    import scripts.run_tests_sharded as runner

    clock = [0.0]
    class FakeProcess:
        def __init__(self, finish):
            self.finish = finish
            self.pid = 1
            self.returncode = None

        def poll(self):
            if clock[0] >= self.finish:
                self.returncode = 0
            return self.returncode

    finishes = iter((0.05, 0.20))
    monkeypatch.setattr(runner, "SHARDS", {"quick": (), "slow": ()})
    monkeypatch.setattr(runner, "validate_shard_coverage", lambda _root: None)
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: FakeProcess(next(finishes)))
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(runner.time, "sleep", lambda duration: clock.__setitem__(0, clock[0] + duration))

    assert runner.run_shards(output_root=tmp_path) == 0
    output = capsys.readouterr().out
    assert "quick: exit=0, elapsed=0.05s" in output
    assert "slow: exit=0, elapsed=0.20s" in output


def test_pytest_cache_is_configured_under_ignored_build_artifacts():
    configuration = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert configuration["tool"]["pytest"]["ini_options"]["cache_dir"] == (
        "build/test-artifacts/pytest-cache"
    )


def test_shard_runner_uses_an_isolated_pytest_cache_per_shard(tmp_path, monkeypatch):
    import sys
    from pathlib import Path

    import scripts.run_tests_sharded as test_runner

    commands = []

    class CompletedProcess:
        pid = 1
        returncode = 0

        def poll(self):
            return 0

    def fake_popen(command, **_kwargs):
        commands.append(command)
        return CompletedProcess()

    monkeypatch.setattr(test_runner, "SHARDS", {"sample": ("tests/test_gui.py",)})
    monkeypatch.setattr(test_runner, "validate_shard_coverage", lambda _root: None)
    monkeypatch.setattr(test_runner.subprocess, "Popen", fake_popen)

    assert test_runner.run_shards(python=Path(sys.executable), output_root=tmp_path) == 0

    command = commands[0]
    assert "no:cacheprovider" not in command
    cache_override = command[command.index("-o") + 1]
    basetemp = Path(command[command.index("--basetemp") + 1])
    assert cache_override == f"cache_dir={basetemp.parent / 'cache'}"


def test_shard_runner_escapes_unencodable_log_characters(monkeypatch):
    import scripts.run_tests_sharded as test_runner

    class AsciiConsole:
        encoding = "ascii"

        def __init__(self):
            self.output = ""

        def write(self, value):
            value.encode(self.encoding)
            self.output += value

    stream = AsciiConsole()

    test_runner.write_console_text("test output: \ufffd", stream)

    assert stream.output == "test output: \\ufffd"


def test_shard_plan_fails_when_a_new_test_module_is_unassigned(tmp_path):
    from scripts.run_tests_sharded import SHARDS

    tests_root = tmp_path / "tests"
    tests_root.mkdir()
    for module in (module for modules in SHARDS.values() for module in modules):
        (tmp_path / module).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / module).write_text("", encoding="utf-8")
    nested_tests = tmp_path / "tests" / "nested"
    nested_tests.mkdir(parents=True)
    (nested_tests / "test_new.py").write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="unassigned test modules"):
        validate_shard_coverage(tmp_path)


def test_web_test_gate_requires_node_22_before_running_the_suite():
    from scripts.run_web_tests import NODE_TEST_FILES, run_web_tests

    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, stdout="v22.11.0\n", stderr="")
        return subprocess.CompletedProcess(command, 0)

    assert run_web_tests(node_executable="node-test-double", runner=fake_run) == 0
    assert calls[0][0] == ["node-test-double", "--version"]
    assert calls[1][0] == ["node-test-double", "--test", *NODE_TEST_FILES]


def test_web_test_gate_rejects_old_node_without_running_tests():
    from scripts.run_web_tests import run_web_tests

    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="v20.19.0\n", stderr="")

    with pytest.raises(RuntimeError, match="Node.js 22 or newer"):
        run_web_tests(node_executable="node-test-double", runner=fake_run)
    assert calls == [["node-test-double", "--version"]]


def test_web_test_gate_returns_the_frontend_test_failure_code():
    from scripts.run_web_tests import run_web_tests

    def fake_run(command, **kwargs):
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, stdout="v24.1.0\n", stderr="")
        return subprocess.CompletedProcess(command, 1)

    assert run_web_tests(node_executable="node-test-double", runner=fake_run) == 1


def test_windows_build_uses_a_ten_minute_pytest_shard_watchdog():
    build_script = (ROOT / "build_windows.ps1").read_text(encoding="utf-8")
    invocation = re.search(r"& \$python \$pytestRunner (?P<arguments>[^\r\n]+)", build_script)

    assert invocation is not None, "Windows build must call the sharded pytest runner directly"
    assert re.search(r"(?:^|\s)--timeout-seconds\s+600(?:\s|$)", invocation.group("arguments")), (
        "Windows packaging must stop the shared pytest shard suite after 600 seconds"
    )


def test_web_node_test_suite_uses_a_bounded_timeout_and_reports_expiration():
    from scripts.run_web_tests import run_web_tests

    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, stdout="v22.11.0\n", stderr="")
        assert kwargs.get("timeout") == 300
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    with pytest.raises(RuntimeError, match="Web frontend JavaScript tests exceeded 300 seconds"):
        run_web_tests(node_executable="node-test-double", runner=fake_run)

    assert len(calls) == 2
    assert calls[1][1]["timeout"] == 300


def test_node_version_probe_has_a_short_timeout_and_reports_expiration():
    from scripts.run_web_tests import run_web_tests

    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        assert command == ["node-test-double", "--version"]
        assert kwargs.get("timeout") == 15
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    with pytest.raises(RuntimeError, match="Node.js version probe exceeded 15 seconds"):
        run_web_tests(node_executable="node-test-double", runner=fake_run)

    assert calls == [
        (
            ["node-test-double", "--version"],
            {
                "cwd": ROOT,
                "capture_output": True,
                "text": True,
                "check": False,
                "timeout": 15,
            },
        )
    ]
