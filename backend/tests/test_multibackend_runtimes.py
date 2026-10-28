"""多后端（CPU / Vulkan / OpenVINO / ROCm）识别与设备枚举。

覆盖 2026-10-27 的实测事实：四个官方 Windows 预编译包是**同一个 llama.cpp
版本**——`--version` 字符串相同、`--help` 里的 GPU 选项逐字相同。因此
"这份构建能用什么加速器"无法从版本或 help 推断，只能靠 `--list-devices`
与目录/DLL 特征识别。

`study/` 下若存在真实预编译包，这些测试直接对它们断言（本机实测数据）；
不存在则跳过——测试不得写死机器路径（AGENTS §6.6），包路径只从常量推导。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from core import runtime_registry, server_probe  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
STUDY = REPO_ROOT / "study"

VULKAN_OUTPUT = """Available devices:
  Vulkan0: AMD Radeon(TM) 610M (16280 MiB, 15466 MiB free)
  Vulkan1: NVIDIA GeForce RTX 5060 Laptop GPU (7899 MiB, 7131 MiB free)
"""

OPENVINO_OUTPUT = """Available devices:
  OPENVINO0: GGML_OPENVINO_DEVICE=CPU (selected) - AMD Ryzen 9 8945HX with Radeon Graphics         (32048 MiB, 13938 MiB free)
  OPENVINO1: GGML_OPENVINO_DEVICE=GPU.0 - NVIDIA GeForce RTX 5060 Laptop GPU (dGPU) (8150 MiB, 8150 MiB free)
  OPENVINO2: GGML_OPENVINO_DEVICE=GPU.1 - gfx1036 (iGPU) (12530 MiB, 12530 MiB free)
"""

EMPTY_OUTPUT = """Available devices:
  (none)
"""


def test_parse_device_list_reads_names_memory_and_free():
    devices = server_probe.parse_device_list(VULKAN_OUTPUT)
    assert [d["name"] for d in devices] == ["Vulkan0", "Vulkan1"]
    assert devices[0]["description"] == "AMD Radeon(TM) 610M"
    assert devices[0]["total_mib"] == 16280
    assert devices[0]["free_mib"] == 15466


def test_parse_device_list_tolerates_openvino_column_padding():
    """OpenVINO 输出用大量空格对齐，正则必须吃得住（实测原始输出即如此）。"""
    devices = server_probe.parse_device_list(OPENVINO_OUTPUT)
    assert [d["name"] for d in devices] == ["OPENVINO0", "OPENVINO1", "OPENVINO2"]
    # 设备名是 --device 的取值；描述里的后端细节原样保留，不改写不猜测
    assert "GGML_OPENVINO_DEVICE=GPU.1" in devices[2]["description"]
    assert devices[2]["total_mib"] == 12530


def test_parse_device_list_returns_empty_for_none_and_garbage():
    assert server_probe.parse_device_list(EMPTY_OUTPUT) == []
    assert server_probe.parse_device_list("") == []
    assert server_probe.parse_device_list("error: something broke") == []


def test_openvino_is_recognized_as_its_own_family():
    """回归：OpenVINO 包曾被判成 unknown。

    四个官方包 `--version` 完全一致，目录名是唯一能分辨 OpenVINO 的线索；
    兄弟 DLL `ggml-openvino.dll` 是权威信号。
    """
    assert runtime_registry._normalize_backend_family("OpenVINO") == "openvino"
    assert runtime_registry._normalize_backend_family("openvino-2026.4.1") == "openvino"
    # 不能把 OpenVINO 误判成 CPU（它同时带 ggml-cpu-* 与 CPU 设备）
    assert runtime_registry._normalize_backend_family("Intel GPU") == "unknown"


def test_rocm_amd_gfx_description_maps_to_rocm():
    assert runtime_registry._normalize_backend_family("AMD gfx1036") == "rocm"
    assert runtime_registry._normalize_backend_family("HIP") == "rocm"


def test_family_inference_uses_sibling_dlls_before_cpu():
    """每个 GPU 包都带 ggml-cpu-*.dll；加速器 DLL 必须先于 CPU 判定。"""
    tmp = Path(__file__).parent / "_infer_probe"
    tmp.mkdir(exist_ok=True)
    try:
        exe = tmp / "llama-server.exe"
        exe.write_bytes(b"stub")
        (tmp / "ggml-openvino.dll").write_bytes(b"stub")
        (tmp / "ggml-cpu-haswell.dll").write_bytes(b"stub")
        probe = server_probe.ServerProbe(path=str(exe), probed=True)
        info = runtime_registry._infer_backend(exe, probe, None)
        assert info["family"] == "openvino"
        assert info["framework"] == "OpenVINO"
    finally:
        for child in tmp.glob("*"):
            child.unlink()
        tmp.rmdir()


def _study_build(name: str) -> Path:
    exe = STUDY / name / "llama-server.exe"
    return exe


@pytest.mark.parametrize(
    ("directory", "family", "framework"),
    [
        ("llama-b11929-bin-win-cpu-x64", "cpu", "CPU"),
        ("llama-b11929-bin-win-vulkan-x64", "vulkan", "Vulkan"),
        ("llama-b11929-bin-win-openvino-2026.4.1-x64", "openvino", "OpenVINO"),
        ("llama-b11929-bin-win-rocm-10.0-x64", "rocm", "ROCm"),
    ],
)
def test_real_study_builds_are_identified_by_family(directory, family, framework):
    """对 study/ 里的真实预编译包断言识别结果（不存在则跳过）。"""
    exe = _study_build(directory)
    if not exe.is_file():
        pytest.skip(f"study 预编译包不存在：{directory}")
    probe = server_probe.probe_server(exe, timeout=30)
    assert probe.probed, f"{directory} 无法执行 --version：{probe.error}"
    info = runtime_registry._infer_backend(exe, probe, None)
    assert info["family"] == family
    assert info["framework"] == framework


def test_real_study_builds_report_device_lists_consistently():
    """设备枚举必须与构建类型自洽：CPU 包空、Vulkan/OpenVINO 有设备。

    ROCm 的期望随机器而变（只有 AMD 受支持独显才有设备），所以只断言
    "结果是一个列表且每项都有 name"——不断言本机是否有 AMD 卡。
    """
    vulkan = _study_build("llama-b11929-bin-win-vulkan-x64")
    cpu = _study_build("llama-b11929-bin-win-cpu-x64")
    rocm = _study_build("llama-b11929-bin-win-rocm-10.0-x64")
    openvino = _study_build("llama-b11929-bin-win-openvino-2026.4.1-x64")
    if not all(p.is_file() for p in (vulkan, cpu, rocm, openvino)):
        pytest.skip("study 预编译包不完整")

    assert server_probe.list_devices(cpu) == [], "CPU 包不应枚举到设备"
    vulkan_devices = server_probe.list_devices(vulkan)
    assert vulkan_devices, "Vulkan 包在本机应至少枚举到一个设备（否则测试机器没有 Vulkan 驱动）"
    assert all(d["name"].startswith("Vulkan") for d in vulkan_devices)

    openvino_devices = server_probe.list_devices(openvino)
    assert openvino_devices, "OpenVINO 包应至少枚举到 CPU 设备"
    assert all(d["name"].startswith("OPENVINO") for d in openvino_devices)

    rocm_devices = server_probe.list_devices(rocm)
    assert isinstance(rocm_devices, list)
    assert all(d.get("name") for d in rocm_devices)


def test_every_study_build_shares_the_same_version_string():
    """记录关键事实：版本串无法区分后端，识别必须靠目录/DLL。

    这条不是"防回归"而是"防误解"——若将来某个构建改了版本串，
    这里会提示识别逻辑仍不依赖它。
    """
    dirs = [
        "llama-b11929-bin-win-cpu-x64",
        "llama-b11929-bin-win-vulkan-x64",
        "llama-b11929-bin-win-openvino-2026.4.1-x64",
        "llama-b11929-bin-win-rocm-10.0-x64",
    ]
    versions = []
    for directory in dirs:
        exe = _study_build(directory)
        if not exe.is_file():
            pytest.skip("study 预编译包不完整")
        probe = server_probe.probe_server(exe, timeout=30)
        versions.append(probe.version)
    assert len(set(versions)) == 1, f"构建版本不再一致（{versions}），可考虑按版本识别"


def test_auto_display_names_distinguish_builds_with_identical_versions(tmp_path, monkeypatch):
    """运行环境列表里三个构建不能都叫同一个版本串。

    2026-10-27 实测：CPU/Vulkan/OpenVINO 三包都注册后，列表显示三行一模一样的
    "0.7.0-dev (build 11929, commit 74a1fd3d0)" —— 用户无从选择，也无法确认
    "设为默认"究竟选中了哪个。自动生成的显示名必须带后端族。
    """
    monkeypatch.setattr(runtime_registry._config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(
        runtime_registry,
        "probe_server",
        lambda path: server_probe.ServerProbe(
            path=str(path),
            probed=True,
            version="0.7.0-dev (build 11929, commit 74a1fd3d0)",
            build="b11929",
            options={},
        ),
    )
    monkeypatch.setattr(runtime_registry.server_probe, "list_devices", lambda path: [])

    names = []
    for family in ("cpu", "vulkan", "openvino"):
        directory = tmp_path / f"llama-b11929-bin-win-{family}-x64"
        directory.mkdir()
        exe = directory / "llama-server.exe"
        exe.write_bytes(b"fake")
        names.append(runtime_registry.register_runtime(str(exe))["display_name"])

    assert len(set(names)) == len(names), f"显示名重复，无法区分构建：{names}"
    assert any("VULKAN" in name for name in names)
    assert any("OPENVINO" in name for name in names)
