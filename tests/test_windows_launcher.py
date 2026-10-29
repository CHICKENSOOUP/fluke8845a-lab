from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import subprocess

import pytest

from scripts import launch_windows


def test_runtime_environment_uses_project_source_and_isolates_external_qt(tmp_path, monkeypatch):
    monkeypatch.setattr(launch_windows, "ROOT", tmp_path)
    monkeypatch.setenv("PYTHONPATH", "existing-dependency-path")
    monkeypatch.setenv("QT_PLUGIN_PATH", "external-qt-plugins")

    environment = launch_windows._runtime_environment()

    assert environment["PYTHONPATH"] == str(tmp_path / "src")
    assert "QT_PLUGIN_PATH" not in environment


@pytest.mark.parametrize(
    ("python_version", "lockfile"),
    [
        ("3.11", "requirements-lock-python311.txt"),
        ("3.12", "requirements-lock.txt"),
    ],
)
def test_first_launch_creates_venv_and_installs_matching_locked_dependencies(
    tmp_path, monkeypatch, python_version, lockfile
):
    monkeypatch.setattr(launch_windows, "ROOT", tmp_path)
    monkeypatch.setattr(launch_windows, "VENV", tmp_path / ".venv")
    venv_python = tmp_path / ".venv" / "Scripts" / "python.exe"
    monkeypatch.setattr(launch_windows, "VENV_PYTHON", venv_python)
    calls: list[list[str]] = []
    import_checks = 0

    def fake_run(command, **kwargs):
        nonlocal import_checks
        calls.append(command)
        if command[1:3] == ["-m", "venv"]:
            venv_python.parent.mkdir(parents=True, exist_ok=True)
            venv_python.write_bytes(b"stub")
            return SimpleNamespace(returncode=0)
        if command[1:2] == ["-c"] and command[2] == launch_windows.RUNTIME_IMPORT_CHECK:
            import_checks += 1
            return SimpleNamespace(returncode=1 if import_checks == 1 else 0)
        if command[1:2] == ["-c"]:
            return SimpleNamespace(returncode=0, stdout=f"{python_version}:64\n")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(launch_windows.sys, "executable", "python-base.exe")
    monkeypatch.setattr(launch_windows.subprocess, "run", fake_run)

    runtime = launch_windows.prepare_runtime()

    assert runtime is not None
    assert runtime[0] == venv_python
    assert import_checks == 2
    assert ["python-base.exe", "-m", "venv", str(tmp_path / ".venv")] in calls
    assert any(
        command[:4] == [str(venv_python), "-m", "pip", "install"]
        and command[-1] == str(tmp_path / lockfile)
        for command in calls
    )


@pytest.mark.parametrize("python_details", ["3.10:64", "3.13:64", "3.12:32"])
def test_launcher_rejects_python_without_a_matching_64_bit_lock(
    tmp_path, monkeypatch, python_details
):
    monkeypatch.setattr(launch_windows, "ROOT", tmp_path)

    def fake_run(command, **kwargs):
        return SimpleNamespace(returncode=0, stdout=f"{python_details}\n")

    monkeypatch.setattr(launch_windows.subprocess, "run", fake_run)

    assert launch_windows._python_lockfile(Path("python.exe"), {}) is None


def test_launcher_selects_desktop_or_web_and_forwards_arguments(monkeypatch, tmp_path):
    python = tmp_path / ".venv" / "Scripts" / "python.exe"
    monkeypatch.setattr(
        launch_windows,
        "prepare_runtime",
        lambda: (python, {"PYTHONPATH": "src"}),
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(launch_windows.subprocess, "run", fake_run)

    assert launch_windows.main(["desktop", "--smoke"]) == 7
    assert launch_windows.main(["web", "--port", "0", "--no-browser"]) == 7
    assert calls[0][0] == [str(python), "-m", "fluke8845a", "--smoke"]
    assert calls[1][0] == [
        str(python), "-m", "fluke8845a.webapp", "--port", "0", "--no-browser"
    ]
    assert all(kwargs["cwd"] == launch_windows.ROOT for _, kwargs in calls)


def test_windowless_launch_prefers_pythonw_for_the_gui_process(tmp_path, monkeypatch):
    python = tmp_path / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    windowed_python = python.with_name("pythonw.exe")
    windowed_python.write_bytes(b"stub")
    monkeypatch.setattr(launch_windows, "WINDOWLESS_LAUNCH", True)

    assert launch_windows._windowless_child(python) == windowed_python


def test_regular_cli_launch_keeps_the_console_python(tmp_path, monkeypatch):
    python = tmp_path / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.with_name("pythonw.exe").write_bytes(b"stub")
    monkeypatch.setattr(launch_windows, "WINDOWLESS_LAUNCH", False)

    assert launch_windows._windowless_child(python) == python


def test_bootstrap_subprocesses_disable_windows_console_creation():
    assert launch_windows._subprocess_options(windows=True) == {
        "creationflags": launch_windows.CREATE_NO_WINDOW
    }
    assert launch_windows._subprocess_options(windows=True, hide_console=False) == {}
    assert launch_windows._subprocess_options(windows=False) == {}


def test_windows_shortcut_scripts_use_hidden_hosts_and_the_app_icon():
    root = Path(__file__).resolve().parents[1]
    installer = (root / "Install-Fluke8845A-Shortcuts.vbs").read_text(encoding="ascii")
    launcher = (root / "scripts" / "launch_app.vbs").read_text(encoding="ascii")

    assert "shortcut.TargetPath = windowlessHost" in installer
    assert 'shortcut.IconLocation = iconPath & ",0"' in installer
    assert '"Fluke 8845A Web"' in installer
    assert 'shell.Run Quote(windowlessHost)' in installer
    assert 'shell.Run(command, 0, True)' in launcher
    assert 'pyw.exe -' in launcher
    assert 'appName <> "desktop" And appName <> "web"' in launcher
    assert "cmd.exe" not in installer.casefold() + launcher.casefold()
    assert ".bat" not in installer.casefold() + launcher.casefold()
