"""后端专属的设备传递约定与失败可见性。

两条都是 2026-10-27 实机实测发现、并已在真机上验证过修复的行为：

1. **OpenVINO 不认 `--device`**。传 `--device OPENVINO1` 时后端直接报错退出：
   "OPENVINO1 is not the selected OpenVINO device (CPU). The OpenVINO device is
   chosen with the GGML_OPENVINO_DEVICE environment variable, not -dev"。
   设备只能经环境变量传入，且取值是设备**描述**里的 `GGML_OPENVINO_DEVICE=` 后缀
   （`OPENVINO1` → `GPU.0`），不是设备名本身。

2. **ROCm 构建在没有可用 AMD 显卡时静默退回 CPU**。实测：`--list-devices` 为
   `(none)`，`-ngl 99` 照收，服务正常 ready，生成约 6.8 tok/s（Vulkan GPU 快一个
   数量级）。启动器必须把这件事说出来，否则用户以为加速生效了。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from core import param_builder, process_manager  # noqa: E402
from core.server_probe import ServerProbe  # noqa: E402

OPENVINO_DEVICES = [
    {
        "name": "OPENVINO0",
        "description": "GGML_OPENVINO_DEVICE=CPU (selected) - AMD Ryzen 9 8945HX with Radeon Graphics",
    },
    {
        "name": "OPENVINO1",
        "description": "GGML_OPENVINO_DEVICE=GPU.0 - NVIDIA GeForce RTX 5060 Laptop GPU (dGPU)",
    },
    {
        "name": "OPENVINO2",
        "description": "GGML_OPENVINO_DEVICE=GPU.1 - gfx1036 (iGPU)",
    },
]


@pytest.mark.parametrize(
    ("device_name", "expected"),
    [("OPENVINO0", "CPU"), ("OPENVINO1", "GPU.0"), ("OPENVINO2", "GPU.1")],
)
def test_openvino_device_name_translates_to_the_env_value(device_name, expected):
    assert process_manager.openvino_device_env_value(device_name, OPENVINO_DEVICES) == expected


def test_openvino_device_translation_refuses_unknown_or_unparseable_names():
    """翻译不出来必须返回空串（调用方据此拒绝启动）。

    猜一个设备等于让用户以为在用 GPU、实际在跑 CPU——正是这次要堵的坑。
    """
    assert process_manager.openvino_device_env_value("OPENVINO9", OPENVINO_DEVICES) == ""
    assert process_manager.openvino_device_env_value("Vulkan0", OPENVINO_DEVICES) == ""
    assert process_manager.openvino_device_env_value("", OPENVINO_DEVICES) == ""
    # 描述里没有 GGML_OPENVINO_DEVICE= 前缀（第三方构建改格式）时同样拒绝
    assert (
        process_manager.openvino_device_env_value(
            "OPENVINO0", [{"name": "OPENVINO0", "description": "some CPU"}]
        )
        == ""
    )


def _build_dir_with(tmp_path: Path, dll_names: tuple[str, ...]) -> Path:
    directory = tmp_path / "build"
    directory.mkdir(parents=True, exist_ok=True)
    exe = directory / "llama-server.exe"
    exe.write_bytes(b"fake")
    for name in dll_names:
        (directory / name).write_bytes(b"")
    return exe


def test_openvino_build_is_detected_by_its_backend_dll(tmp_path):
    openvino = _build_dir_with(tmp_path / "ov", ("ggml-openvino.dll", "ggml-cpu.dll"))
    assert process_manager._is_openvino_build(str(openvino))
    rocm = _build_dir_with(tmp_path / "rocm", ("ggml-hip.dll", "ggml-cpu.dll"))
    assert not process_manager._is_openvino_build(str(rocm))
    assert process_manager._is_rocm_build(str(rocm))
    assert not process_manager._is_rocm_build(str(openvino))
    assert not process_manager._is_rocm_build(None)


def test_device_flag_is_omitted_from_argv_for_openvino_builds(tmp_path):
    """OpenVINO 构建的 argv 里**不得**出现 --device（传了必然启动失败）。

    换个传递通道不等于"参数被丢弃"：它不能出现在 suppressed 列表里，
    否则界面会对用户说"本机构建不支持这个参数"，而实际上设备照常生效。
    """
    exe = _build_dir_with(tmp_path, ("ggml-openvino.dll", "ggml-cpu.dll"))
    probe = ServerProbe(
        path=str(exe),
        probed=True,
        version="0.7.0-dev",
        options={
            "--device": _option("--device"),
            "-m": _option("-m"),
            "--port": _option("--port"),
            "--host": _option("--host"),
        },
    )
    params = {"model": "D:/models/a.gguf", "device": "OPENVINO1", "port": 8080}

    argv = param_builder.build_argv(params, str(exe), probe=probe)
    assert "--device" not in argv
    assert "OPENVINO1" not in argv
    assert "--device" not in param_builder.suppressed_params(params, str(exe), probe=probe)


def test_device_flag_still_reaches_argv_for_vulkan_builds(tmp_path):
    """Vulkan/ROCm 走标准 `--device`，不能被上面的 OpenVINO 特判误伤。"""
    exe = _build_dir_with(tmp_path, ("ggml-vulkan.dll", "ggml-cpu.dll"))
    probe = ServerProbe(
        path=str(exe),
        probed=True,
        version="0.7.0-dev",
        options={
            "--device": _option("--device"),
            "-m": _option("-m"),
            "--port": _option("--port"),
        },
    )
    argv = param_builder.build_argv(
        {"model": "D:/models/a.gguf", "device": "Vulkan0", "port": 8080},
        str(exe),
        probe=probe,
    )
    assert "--device" in argv
    assert "Vulkan0" in argv


def _option(flag: str):
    from core.server_probe import OptionInfo

    return OptionInfo(
        flags=(flag,),
        arg_hint=None,
        default=None,
        allowed_values=(),
        description="",
        deprecated=False,
    )
