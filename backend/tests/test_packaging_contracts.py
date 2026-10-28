"""Static guards for the PyInstaller product.

Source-mode tests cannot catch modules that the frozen build omits. The
2026-10-21 packaged audit found exactly that: ``backend.spec`` excluded PyYAML
while ``model_manifest._load_yaml`` imports it lazily, so every ``model.yaml``
silently became "not found" in the shipped backend.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC = REPO_ROOT / "backend" / "backend.spec"


def test_packaged_backend_includes_lazily_imported_pyyaml():
    source = SPEC.read_text(encoding="utf-8-sig")
    assert "collect_submodules('yaml')" in source
    assert "'yaml', 'PyYAML'" not in source


def test_release_build_rejects_a_missing_or_empty_onedir_runtime():
    """build.rs must validate _internal, not just backend.exe.

    The deployer already checked this, but the NSIS release path goes through
    build.rs. A static guard keeps the release gate from silently regressing.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    source = (root / "src-tauri" / "build.rs").read_text(encoding="utf-8")
    assert "packaged_internal" in source
    assert "non-empty" in source
    assert "read_dir" in source
