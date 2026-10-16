import pytest

from scripts.run_tests_sharded import SHARDS, ROOT, run_shards, validate_shard_coverage


def test_shard_plan_covers_every_python_test_module_once():
    validate_shard_coverage(ROOT)
    modules = [module for shard in SHARDS.values() for module in shard]
    assert len(modules) == len(set(modules))


def test_shard_runner_rejects_timeouts_over_user_limit(tmp_path):
    with pytest.raises(ValueError, match="between 1 and 55"):
        run_shards(timeout_seconds=60, root=tmp_path, output_root=tmp_path / "logs")
    assert not (tmp_path / "logs").exists()


def test_runner_environment_includes_project_src_and_preserves_existing_path(tmp_path):
    import os

    from scripts.run_tests_sharded import runner_environment

    existing = str(tmp_path / "extra-src")
    env = runner_environment(tmp_path, {"PYTHONPATH": existing, "KEEP": "yes"})

    assert env["PYTHONPATH"].split(os.pathsep) == [str(tmp_path / "src"), existing]
    assert env["KEEP"] == "yes"


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
