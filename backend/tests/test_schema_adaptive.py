"""schema 的"构建自适应"输出 + 它与 build_argv 的一致性。

llama-server 的路径由用户在设置里自由指定，构建代次不可控，所以
`/api/params/schema` 不能永远只是一份静态快照：它必须带上"当前指向的构建
认不认这个 flag""上游声明了哪些合法取值"。

这些测试里最重要的一条是 :func:`test_schema_and_build_argv_agree_on_support`
—— 界面按 schema 置灰、命令行按 build_argv 丢弃，两边**必须**是同一条规则，
否则会出现"界面能勾、命令里没有"或者"界面置灰、命令里却有"。
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import core.server_probe as server_probe  # noqa: E402
from core.param_builder import build_argv, suppressed_params  # noqa: E402
from core.param_schema import (  # noqa: E402
    PARAM_DEFINITIONS,
    get_default_params,
    get_schema_for_frontend,
    param_is_supported,
    upstream_options_for,
)
from core.server_probe import (  # noqa: E402
    MIN_COMPLETE_OPTIONS,
    ServerProbe,
    build_probe_from_text,
    clear_server_cache,
)

FAKE_EXE = "llama-server.exe"


def full_probe(*extra: str, drop: tuple[str, ...] = ()) -> ServerProbe:
    """构造一个"完整"的 probe：schema 里的 flag 除 drop 外全都支持。"""
    lines = [f"--filler-{i} N                          filler (default: 0)" for i in range(MIN_COMPLETE_OPTIONS + 5)]
    for m in PARAM_DEFINITIONS:
        if m.cli and m.cli not in drop:
            lines.append(f"{m.cli} N                          flag for {m.key}")
    lines.extend(extra)
    return build_probe_from_text(FAKE_EXE, "\n".join(lines) + "\n")


def param_of(schema: dict, key: str) -> dict:
    return next(p for p in schema["params"] if p["key"] == key)


# ========== supported 三态 ==========


def test_without_a_probe_support_is_unknown():
    """没探测过 → None。前端**不得**据此禁用任何控件。"""
    schema = get_schema_for_frontend()
    assert schema["probed"] is False
    assert schema["probe_complete"] is False
    assert all(p["supported"] is None for p in schema["params"] if p["cli"])


def test_incomplete_probe_does_not_claim_unsupported():
    """解析被截断（选项太少）→ 一律 None：宁可不说，也不能说错。"""
    probe = build_probe_from_text(FAKE_EXE, "--ctx-size N                     ctx (default: 0)\n")
    assert probe.complete is False
    meta = next(m for m in PARAM_DEFINITIONS if m.key == "mlock")
    assert param_is_supported(meta, probe) is None


def test_unsupported_is_reported_for_removed_flags():
    """实测场景：build 10960 删掉了 --mlock 与 --mmap。"""
    probe = full_probe(drop=("--mlock", "--mmap", "--no-mmap"))
    assert param_is_supported(next(m for m in PARAM_DEFINITIONS if m.key == "mlock"), probe) is False
    assert param_is_supported(next(m for m in PARAM_DEFINITIONS if m.key == "mmap"), probe) is False


def test_reverse_flag_is_checked_under_the_name_that_is_actually_written():
    """--mmap=False 真正写出来的是 --no-mmap —— 判的是这个名字。"""
    mmap_meta = next(m for m in PARAM_DEFINITIONS if m.key == "mmap")

    # 只有正向名 --mmap
    assert param_is_supported(mmap_meta, full_probe(drop=("--mmap",))) is False
    # 只有反向名 --no-mmap（真正会被写进命令行的那个）
    assert param_is_supported(mmap_meta, full_probe("--no-mmap      disable mmap", drop=("--mmap",))) is True


def test_non_cli_params_are_always_supported():
    meta = next(m for m in PARAM_DEFINITIONS if m.cli is None)
    assert param_is_supported(meta, full_probe()) is True


# ========== upstream_options ==========


def test_allowed_values_are_forwarded():
    """`--help` 里写了 allowed values 的，如实交给前端。"""
    probe = full_probe("-ctk TYPE                            k type allowed values: f32, f16, q8_0")
    meta = next(m for m in PARAM_DEFINITIONS if m.key == "cache_type_k")
    assert upstream_options_for(meta, probe) == ["f32", "f16", "q8_0"]


def test_spec_type_values_come_from_the_build_not_the_snapshot():
    """--spec-type 的取值写在参数位：新构建多了 draft-dspark，必须能被看到。"""
    probe = build_probe_from_text(
        FAKE_EXE, "--spec-type none,draft-mtp,draft-dspark      speculative decoding\n"
    )
    meta = next(m for m in PARAM_DEFINITIONS if m.key == "spec_type")
    options = upstream_options_for(meta, probe)
    assert options == ["draft-mtp", "draft-dspark"]  # none 由"空选择"表达，不进选项


def test_no_options_when_the_build_says_nothing():
    probe = full_probe()
    meta = next(m for m in PARAM_DEFINITIONS if m.key == "temperature")
    assert upstream_options_for(meta, probe) is None


# ========== 与 build_argv 的一致性（最关键的一条） ==========


def test_schema_and_build_argv_agree_on_support():
    """界面按 schema 置灰、命令行按 build_argv 丢弃 —— 两者必须逐参数一致。

    这是整套自适应最容易走偏的地方：两处各写一份判定，迟早分叉。
    """
    probe = full_probe(drop=("--mlock", "--mmap", "--no-mmap"))
    params = {**get_default_params(), "model": "D:/m.gguf", "mlock": True, "mmap": False}

    argv = build_argv(params, FAKE_EXE, probe=probe)
    for meta in PARAM_DEFINITIONS:
        if not meta.cli:
            continue
        supported = param_is_supported(meta, probe)
        assert supported in (True, False), meta.key
        written = meta.cli in argv or (meta.cli_reverse and f"--no-{meta.cli[2:]}" in argv)
        if supported is False:
            assert not written, f"{meta.key} 被判定不支持，却仍写进了命令行"


# ========== 本轮新增参数（只有新构建有的那些） ==========


def _value_of(argv: list[str], flag: str):
    if flag not in argv:
        return None
    index = argv.index(flag)
    return argv[index + 1] if index + 1 < len(argv) else None


def test_build_only_params_are_dropped_on_older_builds():
    """`--reasoning-effort` / `--load-mode` 只有 build 10960 有（实测）。

    旧构建 → 丢弃 + 留痕（写下去就是 invalid argument）；新构建 → 正常写出。
    """
    params = {
        **get_default_params(), "model": "D:/m.gguf",
        "reasoning_effort": "high", "load_mode": "mlock",
    }

    old_like = full_probe(drop=("--reasoning-effort", "--load-mode"))
    argv_old = build_argv(params, FAKE_EXE, probe=old_like)
    assert "--reasoning-effort" not in argv_old
    assert "--load-mode" not in argv_old
    assert set(suppressed_params(params, FAKE_EXE, probe=old_like)) == {"reasoning_effort", "load_mode"}

    new_like = full_probe()
    argv_new = build_argv(params, FAKE_EXE, probe=new_like)
    assert _value_of(argv_new, "--reasoning-effort") == "high"
    assert _value_of(argv_new, "--load-mode") == "mlock"


def test_new_params_stay_out_of_the_command_until_changed():
    """新增参数的默认值与上游一致（实测 agrees=True）→ 默认不落命令行。

    这条同时是护栏：以后给新参数加 skip_when_default 之前，必须先有探测依据。
    """
    # 这些默认值是照抄两套真实构建的 --help 输出，下面 full_probe 的原文就是证据，
    # 不是推测。新增参数或改动这里之前，请对着真实构建重新核对。
    probe = full_probe(
        "-ctxcp N                          ctx checkpoints (default: 32)",
        "-cms N                            min step (default: 8192)",
        "-cram N                           cache ram (default: 8192)",
        "--cache-reuse N                   reuse (default: 0)",
        "--reasoning-effort LEVEL          effort (default: default)",
        "--load-mode MODE                  load mode (default: auto)",
    )
    argv = build_argv({**get_default_params(), "model": "D:/m.gguf"}, FAKE_EXE, probe=probe)
    for flag in ("-ctxcp", "-cms", "-cram", "--cache-reuse", "--reasoning-effort", "--load-mode"):
        assert flag not in argv, flag

    changed = {**get_default_params(), "model": "D:/m.gguf", "cache_ram": 2048, "ctx_checkpoints": 8}
    argv_changed = build_argv(changed, FAKE_EXE, probe=probe)
    assert _value_of(argv_changed, "-cram") == "2048"
    assert _value_of(argv_changed, "-ctxcp") == "8"


def test_switch_params_are_not_gated_by_the_default_comparison():
    """开关类参数走的是 switch 分支，不进 DefaultGate —— 别指望 skip 对它们生效。

    `kv_unified` / `mmproj_offload` 默认 True → 反向 flag，不输出即等于"用上游默认"。
    """
    params = {**get_default_params(), "model": "D:/m.gguf", "mmproj": "D:/m/mmproj.gguf"}
    argv = build_argv(params, FAKE_EXE, probe=full_probe())
    assert "--no-mmproj-offload" not in argv

    off = {**params, "mmproj_offload": False}
    assert "--no-mmproj-offload" in build_argv(off, FAKE_EXE, probe=full_probe())


# ========== 接口层不许起子进程 ==========


def test_schema_endpoint_never_runs_subprocess(tmp_path, monkeypatch):
    """schema 是页面加载就要拉的接口，绝不能因为它跑一次 --help 而卡住。"""
    clear_server_cache()
    exe = tmp_path / "llama-server.exe"
    exe.write_bytes(b"fake")

    calls: list[tuple] = []

    def fail(*args, **kwargs):
        calls.append(args)
        raise AssertionError(f"schema 触发了同步子进程：{args}")

    monkeypatch.setattr(server_probe.subprocess, "run", fail)

    schema = get_schema_for_frontend(str(exe))
    assert calls == []
    assert schema["probed"] is False  # 缓存里没有 → 如实说"没探测过"


def test_schema_uses_the_cached_probe(tmp_path):
    """缓存里有结果时，schema 应当直接用上（不重新探测）。"""
    clear_server_cache()
    exe = tmp_path / "llama-server.exe"
    exe.write_bytes(b"fake")

    assert get_schema_for_frontend(str(exe))["probed"] is False

    fake_probe = ServerProbe(path=str(exe), probed=True, options={"-c": None})  # 不完整
    server_probe._CACHE[server_probe._cache_key(str(exe))] = fake_probe  # noqa: SLF001

    assert get_schema_for_frontend(str(exe))["probed"] is True
    assert get_schema_for_frontend(str(exe))["probe_complete"] is False
