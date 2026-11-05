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

    The deployer already checked this, but the release path also goes through
    build.rs. A static guard keeps the release gate from silently regressing.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    source = (root / "src-tauri" / "build.rs").read_text(encoding="utf-8")
    assert "packaged_internal" in source
    assert "non-empty" in source
    assert "read_dir" in source


def test_build_rs_does_not_stage_a_bundle_resource_copy():
    """build.rs 只该刷新 target/<profile>/backend，不再往 src-tauri/ 里复制。

    那份 `src-tauri/backend/` staging 副本只服务于 bundle resources（NSIS 安装
    包）。安装包通道移除后没人再读它，留着等于每次构建多复制一份 200 MB 量级的
    运行时，还会让"这个目录为什么存在"失去解释。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    source = (root / "src-tauri" / "build.rs").read_text(encoding="utf-8")
    assert "staging_dir" not in source, "build.rs 又在给 bundle resource 做 staging 了"
    # 开发态仍需要 target/<profile>/backend/backend.exe（外壳按相对路径找 sidecar）
    assert 'join("backend")' in source


def test_build_rs_derives_the_target_dir_from_cargo_not_a_hardcoded_path():
    """sidecar 的落点必须跟随 CARGO_TARGET_DIR。

    README 与 CHANGELOG 都要求把 CARGO_TARGET_DIR 指到仓库外的短路径（在
    `src-tauri/target` 下 ring 的 C 编译会失败）。而 build.rs 曾把目标目录写成
    `manifest_dir/target/<profile>`：指定 CARGO_TARGET_DIR 之后，launcher 产在
    `<CARGO_TARGET_DIR>/<profile>/`，sidecar 却被塞进 `src-tauri/target/<profile>/`。
    两边对不上，启动器找不到 backend.exe，用户看到"后端启动失败"，而这条错误
    完全指不到 build.rs。

    实测（2026-11-05）：修前指定 CARGO_TARGET_DIR=/d/ct2 后
    `/d/ct2/debug/backend/` 不存在；修后存在。所以这里钉的是"必须读 OUT_DIR"。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    source = (root / "src-tauri" / "build.rs").read_text(encoding="utf-8")
    assert 'std::env::var("OUT_DIR")' in source, "build.rs 又用硬编码的 target 路径了"
    # 硬编码的退路只允许作为 fallback，不能是主路径
    hardcoded = source.index('manifest_path.join("target")')
    out_dir = source.index('std::env::var("OUT_DIR")')
    assert out_dir < hardcoded, "OUT_DIR 必须是主路径，硬编码只能作为兜底"
