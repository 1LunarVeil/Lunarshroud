"""「取值等于默认值时能否省略」的三态裁决回归测试。

以前 ``build_argv`` 单方面相信 schema 上的 ``skip_when_default``。那个标记想
表达的是"这个值与 llama-server 自己的默认值一致，写出来是冗余的"，但**这句
话从来没被验证过** —— schema 里写什么就算什么。

现在裁决交给 ``capabilities.compare_param_default`` 的三态结论：

=============  ==========================  ===========================
``agrees``     含义                        行为（本文件逐条断言）
=============  ==========================  ===========================
``True``       schema 默认 == 上游默认      相等时可以省略
``False``      我们有意覆盖上游             **必须**写出来
``None``       上游没声明 / 本机构建不支持    写出来（不为上游代言）
不可用         探测拿不到结果                降级成旧行为，命令行不得变化
=============  ==========================  ===========================

测试里的 probe 一律用 ``build_probe_from_text`` 手工构造 —— 只有手工样本才能
钉死"某个选项到底声明了什么默认值"，靠跑真 exe 是测不准的。
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import core.server_probe as server_probe  # noqa: E402
from core.param_builder import DefaultGate, build_argv, suppressed_params  # noqa: E402
from core.param_schema import PARAM_DEFINITIONS, get_default_params  # noqa: E402
from core.server_probe import (  # noqa: E402
    MIN_COMPLETE_OPTIONS,
    ServerProbe,
    build_probe_from_text,
)

FAKE_EXE = "llama-server.exe"  # 不存在 → 探测必然不可用，用来验证降级


def probe_for(default_text: str) -> ServerProbe:
    """构造一个只声明了 --spec-draft-n-max 默认值的假探测结果。"""
    return build_probe_from_text(
        FAKE_EXE, f"--spec-draft-n-max N                     max draft tokens{default_text}\n"
    )


def complete_probe(*extra: str) -> ServerProbe:
    """选项数够多的 probe。

    "补一个上游反向名"这类**新增**输出只在探测完整时才敢做（`knows_flag`），
    否则猜错就是启动失败。所以测它必须喂够选项数量。
    """
    lines = [f"--filler-{i} N                          filler (default: 0)" for i in range(MIN_COMPLETE_OPTIONS + 5)]
    lines.extend(extra)
    return build_probe_from_text(FAKE_EXE, "\n".join(lines) + "\n")


def spec_params(**overrides) -> dict:
    """激活 draft-mtp 分支，让 --spec-draft-n-max 处于启用状态。"""
    base = get_default_params()
    base.update({"model": "D:/models/main.gguf", "spec_type": ["draft-mtp"], "spec_default": False})
    base.update(overrides)
    return base


def value_of(argv: list[str], flag: str):
    if flag not in argv:
        return None
    index = argv.index(flag)
    return argv[index + 1] if index + 1 < len(argv) else None


# ========== 降级：探测不可用必须与改动前完全一致 ==========


def test_no_probe_degrades_to_the_previous_behaviour():
    """这条是整个改动的**地基**：拿不到探测结果时命令行一个 token 都不能变。

    test_spec_decoding.py::test_defaults_are_not_written_to_the_command_line
    用的也是这个不存在的 exe 路径，它必须继续通过。
    """
    argv_default = build_argv(get_default_params(), FAKE_EXE)
    for flag in ("-s", "-n", "-t", "--rope-scale", "--yarn-orig-ctx", "--rope-scaling"):
        assert flag not in argv_default

    argv_spec = build_argv(spec_params(), FAKE_EXE)
    for flag in ("--spec-draft-n-max", "-ctkd", "-ctvd"):
        assert flag not in argv_spec


def test_golden_argv_is_token_stable_without_a_probe():
    """逐 token 钉住默认参数的命令行 —— 降级路径不允许漂移。"""
    argv = build_argv(get_default_params(), FAKE_EXE)
    # 只剩"我们有意覆盖上游"的那几个 + host/port（后者故意不省，理由见 schema）。
    # 注意这里**没有 -ngl**：n_gpu_layers 的默认值已改为哨兵 -1，即不写本参数、
    # 交给 llama-server 自己适配（显存不够时自动降层而不是崩）。把 -ngl 加回默认
    # 命令行等于重新关掉 llama.cpp 唯一的自救机制，需要显式的理由。
    assert argv[1:] == [
        "--jinja", "-c", "8192", "-np", "1", "-fa", "on",
        "--host", "127.0.0.1", "--port", "8080",
    ]


def test_probe_with_probed_false_is_treated_as_unavailable():
    """probe 对象存在但 probed=False（解析失败/执行失败）也算不可用。"""
    argv = build_argv(spec_params(), FAKE_EXE, probe=ServerProbe(path="p", probed=False))
    assert "--spec-draft-n-max" not in argv


# ========== 三态：True / False / None ==========


def test_agreeing_default_is_omitted():
    """agrees=True：schema 默认与上游一致 → 写出来是冗余的。"""
    argv = build_argv(spec_params(), FAKE_EXE, probe=probe_for(" (default: 3)"))
    assert "--spec-draft-n-max" not in argv


def test_deliberate_override_is_always_written():
    """agrees=False：schema 默认 ≠ 上游默认，这是**有意的覆盖**，必须落进命令行。

    本机反例：flash_attn schema=`on` 而上游 `auto`；parallel schema=1 而上游 -1。
    这类参数即使保持 schema 默认值也必须显式传。
    """
    argv = build_argv(spec_params(), FAKE_EXE, probe=probe_for(" (default: 16)"))
    assert value_of(argv, "--spec-draft-n-max") == "3"


def test_undeclared_upstream_default_is_written():
    """agrees=None（选项存在但没写默认值）→ 不为上游代言，照写。"""
    argv = build_argv(spec_params(), FAKE_EXE, probe=probe_for(""))
    assert value_of(argv, "--spec-draft-n-max") == "3"


def test_option_missing_from_the_build_is_dropped_but_reported():
    """本机构建根本不认这个 flag → **不写**（写下去必崩），但必须留痕。

    这条改过一次，理由是实测出来的：llama.cpp build 10960 删掉了 ``--mlock``
    与 ``--mmap``，实测传进去就是 ``error: invalid argument: --mlock``。
    上一版规则是"不支持也照写，让失败显式发生"，但那会让用户在 UI 里勾的
    开关直接变成启动失败 —— 而这些开关是他从设置界面点的，看不到命令行。
    所以改为：丢弃 + 通过 ``suppressed_params`` 把名单交给调用方展示。
    静默丢弃才是那条永远不能碰的红线。
    """
    probe = _probe_without("--spec-draft-n-max")
    argv = build_argv(spec_params(), FAKE_EXE, probe=probe)
    assert "--spec-draft-n-max" not in argv
    # 同时确认没有被扩大化：该写的还得写
    assert value_of(argv, "--spec-type") == "draft-mtp"
    assert suppressed_params(spec_params(), FAKE_EXE, probe=probe) == ["spec_draft_n_max"]


def test_changed_value_is_written_in_every_state():
    """用户改过值 → 无论三态如何都必须写出来（裁决只在"值==默认值"时生效）。"""
    for probe in (None, probe_for(" (default: 3)"), probe_for(" (default: 16)"), probe_for("")):
        argv = build_argv(spec_params(spec_draft_n_max=7), FAKE_EXE, probe=probe)
        assert value_of(argv, "--spec-draft-n-max") == "7"


# ========== 迁移出硬编码的那几条 ==========


def test_migrated_params_keep_their_old_behaviour_when_upstream_agrees():
    """seed / yarn_orig_ctx / threads / n_predict 原先各有一条硬编码 if。

    本机实测上游确实声明了同样的默认值（`-s -1` / `--yarn-orig-ctx 0` /
    `-t -1` / `-n -1`），所以迁移后行为不变 —— 只是判据从"代码里写死"
    变成了"探测说相等"。
    """
    help_text = "\n".join([
        "-s N                                     seed (default: -1)",
        "--yarn-orig-ctx N                        yarn ctx (default: 0)",
        "-t N                                     threads (default: -1)",
        "-n N                                     predict (default: -1)",
    ])
    probe = build_probe_from_text(FAKE_EXE, help_text)
    params = {**get_default_params(), "rope_scaling": "yarn"}

    argv = build_argv(params, FAKE_EXE, probe=probe)
    for flag in ("-s", "--yarn-orig-ctx", "-t", "-n"):
        assert flag not in argv


def test_rope_scale_is_emitted_because_upstream_declares_no_default():
    """本机的 `--rope-scale` 在 `--help` 里**没写默认值**（实测 agrees=None）。

    旧代码按"看起来像上游默认"把它跳过；按三态规则 None 必须显式输出。
    这是本次**预期内**的命令行变化（llama-server 实测接受 `--rope-scale 1.0`）。
    """
    probe = build_probe_from_text(FAKE_EXE, "--rope-scale N     rope scale factor\n")
    params = {**get_default_params(), "rope_scaling": "yarn"}

    argv = build_argv(params, FAKE_EXE, probe=probe)
    assert value_of(argv, "--rope-scale") == "1.0"


def test_params_that_follow_upstream_are_omitted_when_the_probe_agrees():
    """B 组：schema 只是复刻了上游默认值 → 相等时可以省略。

    覆盖 batch/ubatch、采样组、KV 缓存类型、日志级别。判据不是"看起来像"，
    而是这里构造的 probe 明确声明了同样的默认值。
    """
    lines = [
        "-b N                          batch (default: 2048)",
        "-ub N                         ubatch (default: 512)",
        "--temp N                      temperature (default: 0.80)",
        "--top-k N                     top_k (default: 40)",
        "--top-p N                     top_p (default: 0.95)",
        "--min-p N                     min_p (default: 0.05)",
        "--repeat-penalty N            repeat (default: 1.00)",
        "--repeat-last-n N             repeat_n (default: 64)",
        "-ctk TYPE                     ctk (default: f16)",
        "-ctv TYPE                     ctv (default: f16)",
        "-lv N                         verbosity (default: 3)",
    ]
    probe = build_probe_from_text(FAKE_EXE, "\n".join(lines) + "\n")
    argv = build_argv(get_default_params(), FAKE_EXE, probe=probe)

    for flag in ("-b", "-ub", "--temp", "--top-k", "--top-p", "--min-p",
                 "--repeat-penalty", "--repeat-last-n", "-ctk", "-ctv", "-lv"):
        assert flag not in argv, flag

    # 改过值 → 必须写出来（省略只发生在"值 == 默认值"时）
    changed = {**get_default_params(), "top_k": 20, "cache_type_k": "q8_0"}
    argv_changed = build_argv(changed, FAKE_EXE, probe=probe)
    assert value_of(argv_changed, "--top-k") == "20"
    assert value_of(argv_changed, "-ctk") == "q8_0"


def test_host_and_port_are_written_even_when_the_probe_agrees():
    """host / port 即使探测到与上游一致也照写 —— 护栏，防止后人顺手补上 skip。

    理由：host 是安全边界（非回环必须配 api_key 的校验读的就是它），
    port 是实例身份（健康检查 / Chat iframe / 端口预留全读它）。
    """
    probe = build_probe_from_text(
        FAKE_EXE,
        "--host IP                     host (default: 127.0.0.1)\n"
        "--port N                      port (default: 8080)\n",
    )
    argv = build_argv({**get_default_params(), "model": "D:/m.gguf"}, FAKE_EXE, probe=probe)
    assert value_of(argv, "--host") == "127.0.0.1"
    assert value_of(argv, "--port") == "8080"


def test_sentinel_values_are_not_gated_by_the_default_comparison():
    """n_gpu_layers=-1 是哨兵（交给上游自动判断），不等于它的 schema 默认值 99。

    所以它留在 build_argv 的显式分支里，不受三态裁决影响。
    """
    params = {**get_default_params(), "model": "D:/m.gguf", "n_gpu_layers": "-1"}
    assert "-ngl" not in build_argv(params, FAKE_EXE)

    params["n_gpu_layers"] = "99"
    assert value_of(build_argv(params, FAKE_EXE), "-ngl") == "99"


# ========== 开关类：沉默必须由探测背书 ==========


def test_switch_that_upstream_defaults_on_can_actually_be_turned_off():
    """实测真 bug：`--jinja` 上游默认 enabled，而 schema 默认也是 True。

    用户把「启用 Jinja」关掉时，旧代码什么都不写 → 上游仍然是开的 → **开关失效**。
    有了反向名 `--no-jinja` 就必须显式写出来。
    """
    probe = complete_probe(
        "--jinja, --no-jinja                     enable jinja (default: enabled)",
        "--embeddings                            embed (default: disabled)",
    )
    params = {**get_default_params(), "model": "D:/m.gguf"}

    assert "--jinja" in build_argv(params, FAKE_EXE, probe=probe)

    off = build_argv({**params, "jinja": False}, FAKE_EXE, probe=probe)
    assert "--no-jinja" in off
    assert "--jinja" not in off

    # 上游默认就是关 → 沉默即可，不制造噪音
    assert "--embeddings" not in build_argv(params, FAKE_EXE, probe=probe)


def test_switch_stays_silent_when_upstream_never_declares_a_default():
    """上游没声明默认行为 → 保持沉默：不为它代言，也不凭空加 flag。"""
    probe = complete_probe("--mlock                                 lock memory")
    params = {**get_default_params(), "model": "D:/m.gguf", "mlock": False}
    assert "--mlock" not in build_argv(params, FAKE_EXE, probe=probe)


def test_switch_is_written_explicitly_when_upstream_default_is_conditional():
    """实测反例（真机启动才发现）：`--kv-unified` 的上游默认取决于槽位数是否 auto。

    它的 `--help` 只写 "default: enabled if number of slots is auto"，无法归一化成
    bool。而我们总是显式传 `-np 1`，llama-server 实际打印的是 `kv_unified = 'false'`
    —— 与界面显示的"开"分叉。所以上游没声明时要显式写出意图。
    """
    probe = complete_probe(
        "--kv-unified, --no-kv-unified           unified kv "
        "(default: enabled if number of slots is auto)"
    )
    params = {**get_default_params(), "model": "D:/m.gguf", "kv_unified": True}
    assert "--kv-unified" in build_argv(params, FAKE_EXE, probe=probe)


def test_switch_without_a_reverse_name_is_not_faked():
    """没有反向名时不许硬凑（`--flag off` 未必被接受）→ 保持沉默。"""
    probe = complete_probe("--spec-default                          spec (default: enabled)")
    params = {**get_default_params(), "model": "D:/m.gguf", "spec_default": False}
    argv = build_argv(params, FAKE_EXE, probe=probe)
    assert "--spec-default" not in argv
    assert "--no-spec-default" not in argv


def test_switch_never_invents_a_flag_when_the_probe_is_incomplete():
    """探测不完整时不补反向名 —— 猜错就是启动失败。"""
    probe = build_probe_from_text(
        FAKE_EXE, "--jinja, --no-jinja                     enable jinja (default: enabled)\n"
    )
    params = {**get_default_params(), "model": "D:/m.gguf", "jinja": False}
    argv = build_argv(params, FAKE_EXE, probe=probe)
    assert "--no-jinja" not in argv
    assert "--jinja" not in argv


def test_reverse_switch_turns_itself_on_when_upstream_defaults_off():
    """反向开关（--mmap 风格）：上游默认关而我们想要开 → 显式写正向名。"""
    probe = complete_probe("--mmap, --no-mmap                       memory map (default: disabled)")
    params = {**get_default_params(), "model": "D:/m.gguf", "mmap": True}
    assert "--mmap" in build_argv(params, FAKE_EXE, probe=probe)


# ========== 哨兵取值：声明在 schema 里，代码里没有按参数名的 if ==========


def test_sentinel_values_are_declared_not_hardcoded():
    """`n_gpu_layers=-1`、`rope_scaling=none` 这类"不写出去"的取值来自 schema 声明。

    build_argv 里不该再出现 `meta.key == "…"` 这种按参数名的判断：新增同类参数
    只需在 schema 上标 omit_values。
    """
    n_gpu = next(m for m in PARAM_DEFINITIONS if m.key == "n_gpu_layers")
    n_gpu_draft = next(m for m in PARAM_DEFINITIONS if m.key == "n_gpu_layers_draft")
    rope = next(m for m in PARAM_DEFINITIONS if m.key == "rope_scaling")
    for meta in (n_gpu, n_gpu_draft, rope):
        assert meta.omit_values, meta.key

    params = {**get_default_params(), "model": "D:/m.gguf", "rope_scaling": "yarn",
              "n_gpu_layers": "-1", "n_gpu_layers_draft": "-1"}
    argv = build_argv(params, FAKE_EXE)
    assert "-ngl" not in argv
    assert "-ngld" not in argv

    argv_default = build_argv({**get_default_params(), "model": "D:/m.gguf"}, FAKE_EXE)
    assert value_of(argv_default, "--rope-scaling") is None  # none = 不缩放，写出来是噪音


def test_sentinel_does_not_swallow_real_values():
    params = {**get_default_params(), "model": "D:/m.gguf", "rope_scaling": "yarn",
              "rope_scale": 4.0, "n_gpu_layers": "20"}
    argv = build_argv(params, FAKE_EXE)
    assert value_of(argv, "--rope-scaling") == "yarn"
    assert value_of(argv, "--rope-scale") == "4.0"
    assert value_of(argv, "-ngl") == "20"


# ========== 本机构建不支持的 flag：丢弃 + 留痕 ==========


def _probe_without(*missing: str) -> ServerProbe:
    """模拟一个构建：schema 里的 flag **除了 missing** 全都支持。

    两个约束：
    * 选项数要够（≥100）—— ``supported()`` 只在探测结果完整时才敢丢弃参数，
      否则 --help 解析被截断时会把整条命令行清空；
    * 除 missing 外的真实 flag 都要在 —— 否则它们也会被判"不支持"，
      测出来的就是"全丢"而不是"精准丢这一个"。
    """
    lines = [f"--opt-{index} N                          filler (default: 0)" for index in range(120)]
    for m in PARAM_DEFINITIONS:
        if m.cli and m.cli not in missing:
            lines.append(f"{m.cli} N                          flag for {m.key}")
    return build_probe_from_text(FAKE_EXE, "\n".join(lines) + "\n")


def _new_build_probe() -> ServerProbe:
    """模拟实测的那个新构建：--mmap / --no-mmap / --mlock 都被删掉了。"""
    return _probe_without("--mmap", "--no-mmap", "--mlock")


def test_switch_dropped_when_the_build_removed_it():
    """实测场景：用户勾了「锁定内存」，但指向的构建已经没有 --mlock。"""
    params = {**get_default_params(), "model": "D:/m.gguf", "mlock": True}
    probe = _new_build_probe()

    argv = build_argv(params, FAKE_EXE, probe=probe)
    assert "--mlock" not in argv
    assert suppressed_params(params, FAKE_EXE, probe=probe) == ["mlock"]


def test_reverse_switch_dropped_when_the_build_removed_it():
    """反向 flag 判的是真正会写出的那个名字（--mmap=False → --no-mmap）。"""
    params = {**get_default_params(), "model": "D:/m.gguf", "mmap": False}
    probe = _new_build_probe()

    argv = build_argv(params, FAKE_EXE, probe=probe)
    assert "--no-mmap" not in argv
    assert suppressed_params(params, FAKE_EXE, probe=probe) == ["mmap"]


def test_nothing_is_reported_when_the_value_would_not_be_written_anyway():
    """默认关闭的开关本来就不产出 flag —— 不该被误报成"被抑制"。"""
    params = {**get_default_params(), "model": "D:/m.gguf"}  # mlock=False / mmap=True
    assert suppressed_params(params, FAKE_EXE, probe=_new_build_probe()) == []


def test_supported_flags_are_untouched():
    """构建支持的 flag 不受这条规则影响（别把"不支持"判定扩大化）。"""
    params = {**get_default_params(), "model": "D:/m.gguf", "mlock": True}
    probe = _probe_without()  # 全都支持

    assert "--mlock" in build_argv(params, FAKE_EXE, probe=probe)
    assert suppressed_params(params, FAKE_EXE, probe=probe) == []


def test_incomplete_probe_never_drops_anything():
    """`--help` 解析被截断（选项很少）时不按"不支持"丢弃 —— 否则命令行会被清空。

    这是加丢弃逻辑时最容易忽略的一面：宁可不丢弃，也不能因为解析不全而
    把整条命令拆掉。
    """
    probe = build_probe_from_text(FAKE_EXE, "--ctx-size N                          ctx (default: 0)\n")
    params = {**get_default_params(), "model": "D:/m.gguf", "mlock": True}

    assert "--mlock" in build_argv(params, FAKE_EXE, probe=probe)
    assert suppressed_params(params, FAKE_EXE, probe=probe) == []


# ========== 高频路径：绝不能在命令预览里同步跑 --help ==========


def test_hot_path_never_runs_subprocess(monkeypatch):
    """命令预览接口每次输入都会调 build_argv —— 里面不许有同步子进程。

    调用方传进 probe（真实路径走 probe_server_cached_only 的缓存）后，重复
    调用不得再碰 subprocess。
    """
    calls: list[tuple] = []

    def fail(*args, **kwargs):
        calls.append(args)
        raise AssertionError(f"build_argv 触发了同步子进程：{args}")

    monkeypatch.setattr(server_probe.subprocess, "run", fail)

    probe = probe_for(" (default: 3)")
    for _ in range(3):
        build_argv(spec_params(), FAKE_EXE, probe=probe)

    assert calls == []


def test_cold_cache_never_runs_subprocess(tmp_path, monkeypatch):
    """缓存里没有结果时**不许自己补探** —— 这是本次改动最要紧的一条约束。

    反例已经真实发生过一次：e2e 冒烟测试把 llama_server_path 指向一个不会
    自行退出的假 server，只要 build_argv 同步跑一次 --help，每次调用都要等
    到 15s 超时（--help 与 --version 各一次），预览接口和真实启动一起被拖死。
    """
    from core.server_probe import clear_server_cache

    clear_server_cache()
    exe = tmp_path / "llama-server.exe"
    exe.write_bytes(b"fake")

    calls: list[tuple] = []

    def fail(*args, **kwargs):
        calls.append(args)
        raise AssertionError(f"build_argv 触发了同步子进程：{args}")

    monkeypatch.setattr(server_probe.subprocess, "run", fail)

    argv = build_argv(spec_params(), str(exe))

    assert calls == []
    # 缓存冷 → 降级 → 与改动前完全一致
    assert "--spec-draft-n-max" not in argv


def test_probe_run_uses_create_no_window(tmp_path, monkeypatch):
    """冷探测的子进程必须带 CREATE_NO_WINDOW。

    打包态 backend.exe 是无控制台程序（backend.spec console=False），探测
    不带这个 flag 时，每次启动预热（--help / --version 各一次）都会给
    llama-server 弹一个可见控制台窗口 —— 用户看到的就是"闪命令行"。
    """
    import os
    import types

    exe = tmp_path / "llama-server.exe"
    exe.write_bytes(b"fake")
    captured: list[dict] = []

    def fake_run(*args, **kwargs):
        captured.append(kwargs)
        return types.SimpleNamespace(stdout=b"usage: llama-server\n", stderr=b"")

    monkeypatch.setattr(server_probe.subprocess, "run", fake_run)
    server_probe.probe_server(str(exe))

    assert len(captured) == 2  # --help 与 --version 各一次
    expected = getattr(server_probe.subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    for kwargs in captured:
        assert kwargs.get("creationflags") == expected


def test_gate_decides_for_every_marked_param():
    """每个标了 skip_when_default 的参数都必须真的被裁决，一个都不能漏。

    两种情形都要覆盖：
    * 上游声明了同样的默认值 → 三态裁决为 True → 省略
    * 探测不可用 → 降级 → 与改动前完全一致（省略）
    """
    marked = [m for m in PARAM_DEFINITIONS if m.skip_when_default]
    assert len(marked) >= 9  # 原来的 9 条 + 本次从硬编码迁进来的几条

    lines = [f"{m.cli} N   something (default: {m.default})" for m in marked]
    probe = build_probe_from_text(FAKE_EXE, "\n".join(lines))

    gate = DefaultGate(FAKE_EXE, probe)
    assert gate.probes_available is True
    for m in marked:
        assert gate.omit(m, m.default) is True, m.key
        assert gate.omit(m, "changed-on-purpose") is False, m.key

    degraded = DefaultGate(FAKE_EXE, None)
    assert degraded.probes_available is False
    for m in marked:
        assert degraded.omit(m, m.default) is True, m.key
