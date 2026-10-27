"""Run the complete pytest suite in independently logged parallel shards."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHARDS = {
    "desktop": (
        "tests/test_gui.py",
    ),
    "runtime": (
        "tests/test_app.py",
        "tests/test_scpi.py",
        "tests/test_hardware_validation_script.py",
        "tests/test_webapp.py",
        "tests/test_web_chart_history.py",
        "tests/test_web_share_export_endpoint.py",
        "tests/test_worker.py",
    ),
    "release": (
        "tests/test_build_identity.py",
        "tests/test_build_environment.py",
        "tests/test_build_provenance.py",
        "tests/test_hardware_probe_manifest.py",
        "tests/test_latest_verified_launcher.py",
        "tests/test_package_release.py",
        "tests/test_runtime_licenses.py",
        "tests/test_share_export.py",
        "tests/test_windows_launcher.py",
    ),
    "core": (
        "tests/test_analysis.py",
        "tests/test_history_import.py",
        "tests/test_icon_builder.py",
        "tests/test_filter_policy.py",
        "tests/test_mock.py",
        "tests/test_modes.py",
        "tests/test_path_security.py",
        "tests/test_plot_history.py",
        "tests/test_state.py",
        "tests/test_iv_backend.py",
        "tests/test_storage.py",
        "tests/test_test_shards.py",
    ),
}


def validate_shard_coverage(root: Path = ROOT) -> None:
    """Require every collected test module to occur exactly once in the plan."""
    listed = [module for modules in SHARDS.values() for module in modules]
    if len(listed) != len(set(listed)):
        raise ValueError("pytest shard plan contains a duplicate module")
    discovered = {
        path.relative_to(root).as_posix()
        for path in (root / "tests").rglob("test_*.py")
    } | {
        path.relative_to(root).as_posix()
        for path in (root / "tests").rglob("*_test.py")
    }
    missing = sorted(discovered - set(listed))
    stale = sorted(set(listed) - discovered)
    if missing or stale:
        parts = []
        if missing:
            parts.append("unassigned test modules: " + ", ".join(missing))
        if stale:
            parts.append("missing shard modules: " + ", ".join(stale))
        raise ValueError("; ".join(parts))


def _stop_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=4,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def runner_environment(root: Path = ROOT, base: dict[str, str] | None = None) -> dict[str, str]:
    """Add this checkout's source tree while preserving caller environment paths."""
    env = dict(os.environ if base is None else base)
    src = str((Path(root).resolve() / "src").resolve())
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(part for part in (src, existing) if part)
    return env


def default_output_root() -> Path:
    """Keep pytest logs and temporary files out of the source checkout."""
    return Path(tempfile.gettempdir()) / "Fluke8845A_Test_Artifacts" / "pytest-shards"


def write_console_text(value: str, stream=None) -> None:
    """Write diagnostics even when a Windows console encoding cannot represent a log character."""
    stream = stream or sys.stdout
    encoding = getattr(stream, "encoding", None) or "utf-8"
    stream.write(value.encode(encoding, errors="backslashreplace").decode(encoding))


def run_shards(
    *,
    python: Path = Path(sys.executable),
    timeout_seconds: int | None = None,
    root: Path = ROOT,
    output_root: Path | None = None,
) -> int:
    if timeout_seconds is not None and timeout_seconds < 1:
        raise ValueError("timeout-seconds must be positive when specified")
    root = Path(root).resolve()
    validate_shard_coverage(root)
    output_root = Path(output_root) if output_root is not None else default_output_root()
    run_directory = output_root / uuid.uuid4().hex
    run_directory.mkdir(parents=True, exist_ok=False)

    running: dict[str, tuple[subprocess.Popen, object, object, float]] = {}
    deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
    try:
        for name, modules in SHARDS.items():
            shard_directory = run_directory / name
            shard_directory.mkdir()
            stdout_stream = (shard_directory / "stdout.log").open("wb")
            stderr_stream = (shard_directory / "stderr.log").open("wb")
            command = [
                str(Path(python).resolve()), "-m", "pytest",
                "--maxfail=1", "--basetemp", str(shard_directory / "temp"),
                "-o", f"cache_dir={shard_directory / 'cache'}",
                *modules,
            ]
            try:
                process = subprocess.Popen(
                    command,
                    cwd=root,
                    stdout=stdout_stream,
                    stderr=stderr_stream,
                    stdin=subprocess.DEVNULL,
                    env=runner_environment(root),
                )
            except BaseException:
                stdout_stream.close()
                stderr_stream.close()
                raise
            running[name] = (process, stdout_stream, stderr_stream, time.monotonic())

        while any(process.poll() is None for process, *_rest in running.values()):
            if deadline is not None and time.monotonic() >= deadline:
                processes = [process for process, *_rest in running.values()]
                with ThreadPoolExecutor(max_workers=len(processes)) as executor:
                    list(executor.map(_stop_process_tree, processes))
                for name in running:
                    print(f"{name}: exceeded {timeout_seconds}s; process tree stopped")
                print(f"pytest shard logs: {run_directory}")
                return 124
            time.sleep(0.05)

        failed = False
        for name, (process, stdout_stream, stderr_stream, started) in running.items():
            elapsed = time.monotonic() - started
            stdout_stream.close()
            stderr_stream.close()
            code = int(process.returncode or 0)
            print(f"{name}: exit={code}, elapsed={elapsed:.2f}s")
            for log_name in ("stdout.log", "stderr.log"):
                log_path = run_directory / name / log_name
                content = log_path.read_bytes()
                if content:
                    print(f"--- {name}/{log_name} ---")
                    write_console_text(content.decode("utf-8", errors="replace"))
                    if not content.endswith(b"\n"):
                        sys.stdout.write("\n")
            failed |= code != 0
        print(f"pytest shard logs: {run_directory}")
        return 1 if failed else 0
    finally:
        for process, stdout_stream, stderr_stream, _started in running.values():
            if process.poll() is None:
                _stop_process_tree(process)
            for stream in (stdout_stream, stderr_stream):
                if not stream.closed:
                    stream.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, default=Path(sys.executable), help="Python executable with pytest installed")
    parser.add_argument("--timeout-seconds", type=int, default=None, help="optional shared watchdog for all parallel test shards; omitted by default")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output-root", type=Path, help="directory for unique per-run shard logs and pytest basetemp")
    args = parser.parse_args(argv)
    try:
        return run_shards(
            python=args.python,
            timeout_seconds=args.timeout_seconds,
            root=args.root,
            output_root=args.output_root,
        )
    except (OSError, ValueError) as exc:
        print(f"Unable to start complete pytest shard suite: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
