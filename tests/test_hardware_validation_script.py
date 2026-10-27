from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "validate_res2_nplc_speed.py"
CONFIRM_ARGUMENT = "--confirm-front-2w-30-megaohm"


def _parse_script_arguments(monkeypatch, *arguments: str):
    monkeypatch.setattr(sys, "argv", [str(SCRIPT_PATH), *arguments, CONFIRM_ARGUMENT])
    script_globals = runpy.run_path(str(SCRIPT_PATH), run_name="res2_validation_argument_test")
    return script_globals["_arguments"]()


@pytest.mark.parametrize("duration", ["60.001", "3600"])
def test_real_res2_validation_rejects_duration_above_one_minute(
    monkeypatch, capsys, duration,
):
    with pytest.raises(SystemExit) as exc_info:
        _parse_script_arguments(monkeypatch, "--duration", duration)

    assert exc_info.value.code == 2
    assert "duration must not exceed 60 seconds" in capsys.readouterr().err


def test_real_res2_validation_accepts_exactly_one_minute(monkeypatch):
    args = _parse_script_arguments(monkeypatch, "--duration", "60")

    assert args.duration == 60.0
