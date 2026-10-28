"""设置页 llama-server 路径 ↔ 运行环境默认项的**双向**同步。

为什么需要这一组测试
--------------------
"用哪个 llama-server"在代码里有两个视图：``config.llama_server_path``（设置页
的裸路径）与运行环境注册表的 ``defaults.gguf``（``/api/runtimes`` 列表 + 默认项）。
只镜像一个方向会分叉：设置页改了路径、注册表默认项仍是旧构建 → 用户以为换成了
新构建，实际启动用的还是旧的，界面上看不出任何异常。

两个方向：
* 运行环境页选默认 → 镜像到 ``llama_server_path``（``sync_default_to_config_path``）
* 设置页填路径   → 镜像到注册表默认项（``sync_config_path_to_default_preference``）
"""
from __future__ import annotations

import pytest
from core import runtime_registry
from core.server_probe import ServerProbe


def _probe(path: str) -> ServerProbe:
    return ServerProbe(
        path=path,
        probed=True,
        version="0.7.0-dev (build 11929)",
        build="b11929",
        options={},
    )


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_registry._config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runtime_registry, "probe_server", lambda path: _probe(str(path)))
    # list_devices 会起子进程探 --list-devices；注册表测试不关心设备
    monkeypatch.setattr(runtime_registry.server_probe, "list_devices", lambda path: [])
    return runtime_registry


def _make_exe(tmp_path, name: str):
    directory = tmp_path / name
    directory.mkdir()
    exe = directory / "llama-server.exe"
    exe.write_bytes(b"fake")
    return exe


def test_setting_the_path_mirrors_into_the_registry_default(registry, tmp_path):
    """设置页写路径 → 注册表默认项跟随（本次新增的反向镜像）。"""
    vulkan = _make_exe(tmp_path, "vulkan")
    cuda = _make_exe(tmp_path, "cuda")
    vulkan_profile = registry.register_runtime(str(vulkan))
    cuda_profile = registry.register_runtime(str(cuda))
    registry.set_runtime_preferences({"defaults": {"gguf": cuda_profile["id"]}})

    synced = registry.sync_config_path_to_default_preference(str(vulkan))

    assert synced == vulkan_profile["id"]
    assert registry.get_runtime_preferences()["defaults"]["gguf"] == vulkan_profile["id"]


def test_syncing_an_already_default_path_is_a_no_op(registry, tmp_path):
    exe = _make_exe(tmp_path, "only")
    profile = registry.register_runtime(str(exe))
    registry.set_runtime_preferences({"defaults": {"gguf": profile["id"]}})

    assert registry.sync_config_path_to_default_preference(str(exe)) is None


def test_syncing_an_unregistered_path_does_not_invent_a_profile(registry, tmp_path):
    """未注册路径不得触发自动注册。

    注册会跑一次 ``--help``（还有 ``--list-devices``）探测；把它塞进配置保存的
    同步路径，会让"只改个端口"也顺带起两个子进程。未注册路径仍能正常启动
    （``_runtime_for_launch`` 在无默认项时回退到配置路径）。
    """
    stray = _make_exe(tmp_path, "stray")

    assert registry.sync_config_path_to_default_preference(str(stray)) is None
    assert registry.list_runtime_profiles() == []
    assert registry.get_runtime_preferences()["defaults"] == {}


def test_syncing_empty_or_missing_path_is_a_no_op(registry, tmp_path):
    exe = _make_exe(tmp_path, "real")
    profile = registry.register_runtime(str(exe))
    registry.set_runtime_preferences({"defaults": {"gguf": profile["id"]}})

    assert registry.sync_config_path_to_default_preference("") is None
    assert registry.sync_config_path_to_default_preference("   ") is None
    assert registry.sync_config_path_to_default_preference(str(tmp_path / "nope.exe")) is None
    assert registry.get_runtime_preferences()["defaults"]["gguf"] == profile["id"]


def test_setting_the_registry_default_mirrors_into_the_path(registry, tmp_path):
    """反向：运行环境页选默认 → 设置页路径（既有行为，本条钉住成对关系）。"""
    exe = _make_exe(tmp_path, "rocm")
    profile = registry.register_runtime(str(exe))

    mirrored = registry.sync_default_to_config_path({"defaults": {"gguf": profile["id"]}})

    assert mirrored == str(exe)


def test_both_directions_converge_on_the_same_build(registry, tmp_path):
    """两个方向交替执行后，两个视图必须指向同一个可执行文件（不漂移）。"""
    first = _make_exe(tmp_path, "cpu")
    second = _make_exe(tmp_path, "openvino")
    first_profile = registry.register_runtime(str(first))
    second_profile = registry.register_runtime(str(second))

    # 用户先在设置页填了 first
    registry.sync_config_path_to_default_preference(str(first))
    assert registry.sync_default_to_config_path(registry.get_runtime_preferences()) == str(first)

    # 再在运行环境页把默认改成 second
    registry.set_runtime_preferences({"defaults": {"gguf": second_profile["id"]}})
    assert registry.sync_default_to_config_path(registry.get_runtime_preferences()) == str(second)

    # 又把设置页改回 first
    registry.sync_config_path_to_default_preference(str(first))
    assert registry.get_runtime_preferences()["defaults"]["gguf"] == first_profile["id"]
    assert registry.sync_default_to_config_path(registry.get_runtime_preferences()) == str(first)
