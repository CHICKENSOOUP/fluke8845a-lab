from __future__ import annotations

import json
from hashlib import sha256

from fluke8845a.build_identity import desktop_build_label


def test_desktop_build_label_uses_packaged_source_digest(tmp_path):
    executable = tmp_path / "Fluke8845A.exe"
    executable_bytes = b"frozen executable"
    executable.write_bytes(executable_bytes)
    (tmp_path / "BUILD-PROVENANCE.json").write_text(
        json.dumps({
            "source_tree_sha256": "3CCF61D761B49A127E1A5EB69415ED780138ECB42CD07E8BEE5A855332C4BC05",
            "executables": {"desktop": {"sha256": sha256(executable_bytes).hexdigest()}},
        }),
        encoding="utf-8",
    )

    assert desktop_build_label(executable, "1.1.0", frozen=True) == "v1.1.0 · 构建 3CCF61D7"


def test_desktop_build_label_does_not_claim_an_identity_without_a_valid_manifest(tmp_path):
    executable = tmp_path / "Fluke8845A.exe"
    executable.write_bytes(b"frozen executable")

    assert desktop_build_label(executable, "1.1.0", frozen=True) == "v1.1.0 · 构建标识缺失"

    (tmp_path / "BUILD-PROVENANCE.json").write_text(
        json.dumps({
            "source_tree_sha256": "not-a-sha256",
            "executables": {"desktop": {"sha256": "0" * 64}},
        }),
        encoding="utf-8",
    )
    assert desktop_build_label(executable, "1.1.0", frozen=True) == "v1.1.0 · 构建标识缺失"


def test_desktop_build_label_reports_executable_and_manifest_mismatch(tmp_path):
    executable = tmp_path / "Fluke8845A.exe"
    executable.write_bytes(b"new executable")
    (tmp_path / "BUILD-IDENTITY.json").write_text(
        json.dumps({
            "source_tree_sha256": "3CCF61D761B49A127E1A5EB69415ED780138ECB42CD07E8BEE5A855332C4BC05",
            "executables": {"desktop": {"sha256": "0" * 64}},
        }),
        encoding="utf-8",
    )

    assert desktop_build_label(executable, "1.1.0", frozen=True) == "v1.1.0 · 构建标识不匹配"


def test_desktop_build_label_marks_non_frozen_source_runs(tmp_path):
    assert desktop_build_label(tmp_path / "python.exe", "1.1.0", frozen=False) == "v1.1.0 · 源码运行"
