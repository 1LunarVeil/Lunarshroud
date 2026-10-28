"""llama-server 能力探测测试。

夹具用的是**从真实 ``--help`` 摘录的排版**（含列 0 = 新选项、多字符短选项、
各种 default 变体），而不是我理想中的格式 —— 解析器的难点全在这些坑里。
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from core.server_probe import (  # noqa: E402
    ServerProbe,
    extract_value_list,
    parse_help,
    parse_spec_types,
    parse_version,
    probe_server,
    split_blocks,
    split_flags,
)

# 照抄本机 llama-server --help 的真实排版：选项一律在**列 0**，
# 缩进的是续行。节标题（----- common params -----）也在列 0，但不是选项。
HELP_SAMPLE = """usage: llama-server [options]

----- common params -----

-h,    --help, --usage                  print usage and exit
--version                               show version and build info
-cl,   --cache-list                     show list of models in cache
-t,    --threads N                      number of CPU threads to use during generation (default: -1)
                                        (env: LLAMA_ARG_THREADS)
-c,    --ctx-size N                     size of the prompt context (default: 0, 0 = loaded from model)
-ctk,  --cache-type-k TYPE              KV cache data type for K
                                        allowed values: f32, f16, bf16, q8_0, q4_0
                                        (default: f16)
-ngl,  --gpu-layers, --n-gpu-layers N   max. number of layers to store in VRAM, either an exact number,
                                        'auto', or 'all' (default: auto)
-fa,   --flash-attn [on|off|auto]       set Flash Attention use ('on', 'off', or 'auto', default: 'auto')
--perf, --no-perf                       whether to enable internal libllama performance timings (default:
                                        disabled)
--spec-draft-type-k, -ctkd, --cache-type-k-draft TYPE
                                        KV cache data type for K for the draft model
                                        (default: f16)
--spec-type none,draft-simple,draft-eagle3,draft-mtp,draft-dflash,ngram-simple,ngram-map-k,ngram-map-k4v,ngram-mod,ngram-cache
                                        comma-separated list of types of speculative decoding to use (default:
                                        none)
                                        (env: LLAMA_ARG_SPEC_TYPE)
-dt,   --defrag-thold N                 KV cache defragmentation threshold (DEPRECATED)
--threads-batch, -tb, --threads-batch-draft N
                                        number of threads to use during batch and prompt processing (default:
                                        same as --threads)
--sse-ping-interval N                   server SSE ping interval in seconds (-1 = disabled, default: 30)
--chat-template JINJA_TEMPLATE          template for the chat (default: template taken from model's metadata)
--pooling {none,mean,cls,last,rank}     pooling type for embeddings, use model default if unspecified
"""


# ========== 切块与选项别名 ==========


def test_split_blocks_treats_column_zero_as_new_option():
    blocks = split_blocks(HELP_SAMPLE)
    threads = next(b for b in blocks if b.startswith("-t,"))
    # 续行跟着自己的选项走，不会跨过下一条列 0 的行
    assert "LLAMA_ARG_THREADS" in threads
    assert "common params" not in threads


def test_preamble_and_section_headers_do_not_become_options():
    """前言与节标题也在列 0，会各自成块，但**建不出任何选项键**。"""
    blocks = split_blocks(HELP_SAMPLE)
    assert any(b.startswith("usage:") for b in blocks)
    assert any(b.startswith("-----") for b in blocks)

    options = parse_help(HELP_SAMPLE)
    assert options
    assert all(k.startswith("-") for k in options)
    # 每个选项名都指回同一批 OptionInfo，没有多出来的
    assert len(blocks) == len({id(v) for v in options.values()}) + 2  # +2 = 前言 + 节标题
    assert options["-h"].description == "print usage and exit"


def test_split_flags_keeps_multi_char_short_options():
    """`-ctk,` 曾被截成 `-c`（正则只吃 1 个字符），导致该选项建不出键、
    还会用截断出的 `-c` 覆盖别的条目。这是一条回归守卫。"""
    flags, rest = split_flags("-ctk,  --cache-type-k TYPE              KV cache data type for K")
    assert flags == ["-ctk", "--cache-type-k"]
    assert rest.startswith("TYPE")

    flags, _ = split_flags("-ngl,  --gpu-layers, --n-gpu-layers N   max. number of layers")
    assert flags == ["-ngl", "--gpu-layers", "--n-gpu-layers"]

    flags, _ = split_flags("--spec-draft-type-k, -ctkd, --cache-type-k-draft TYPE")
    assert flags == ["--spec-draft-type-k", "-ctkd", "--cache-type-k-draft"]


def test_split_flags_stops_at_argument_placeholder():
    flags, rest = split_flags("-fa,   --flash-attn [on|off|auto]       set Flash Attention use")
    assert flags == ["-fa", "--flash-attn"]
    assert rest.startswith("[on|off|auto]")


# ========== 默认值解析的各种排版 ==========


def test_option_defaults_parse_all_real_layouts():
    options = parse_help(HELP_SAMPLE)

    # 直给
    assert options["-t"].default == "-1"
    assert options["-ctk"].default == "f16"
    # 逗号后的注解要丢掉
    assert options["-c"].default == "0"
    # 值被折到下一行
    assert options["--perf"].default == "disabled"
    # default 不在括号开头
    assert options["--sse-ping-interval"].default == "30"
    # 带引号要去掉
    assert options["-fa"].default == "auto"
    # 描述性文字 / 引用一律 None，不许编造
    assert options["--threads-batch"].default is None
    assert options["--chat-template"].default is None
    # 没有 default 的选项
    assert options["-h"].default is None


def test_default_is_none_when_only_a_phrase_is_given():
    text = "--chat-template JINJA_TEMPLATE      template for the chat (default: template taken from model's metadata)\n"
    options = parse_help(text)
    assert options["--chat-template"].default is None


def test_multivalue_default_keeps_the_whole_list():
    """逗号既可能是"列表分隔"也可能是"注解分隔"，两种都必须对：

    * ``(default: ngram-mod,ngram-cache)``   → 两个值都是默认值
    * ``(default: 1.00, 1.0 = disabled)``    → 只有 1.00 是

    第一版无条件截断到第一个逗号，会把多值默认值**静默截掉一半**。
    """
    text = "--spec-type A                        spec types (default: ngram-mod,ngram-cache)\n"
    assert parse_help(text)["--spec-type"].default == "ngram-mod,ngram-cache"

    text2 = "--repeat-penalty N                   penalize repeats (default: 1.00, 1.0 = disabled)\n"
    assert parse_help(text2)["--repeat-penalty"].default == "1.00"

    # 等号左侧才是值的那种只可能出现在第一段
    text3 = "-c N                                 context size (default: 0 = loaded from model)\n"
    assert parse_help(text3)["-c"].default == "0"


def test_allowed_values_are_parsed():
    options = parse_help(HELP_SAMPLE)
    assert options["-ctk"].allowed_values == ("f32", "f16", "bf16", "q8_0", "q4_0")
    assert options["-t"].allowed_values == ()


def test_allowed_values_are_parsed_from_the_argument_position():
    """上游还有两种把取值写在**参数位**的排版：``{a,b,c}`` 与 ``[a|b|c]``。

    只认 ``allowed values:`` 文本时这两类的取值拿不到，前端就只能吃 schema 的
    静态快照 —— 而 schema 是快照、上游会新增取值（``--spec-type`` 已经发生过
    一次）。实测 v0.7.0：``--pooling`` / ``--rope-scaling`` 用花括号，
    ``--flash-attn`` / ``--reasoning`` / ``--fit`` / ``--log-colors`` 用方括号。
    """
    options = parse_help(HELP_SAMPLE)
    assert options["--pooling"].allowed_values == ("none", "mean", "cls", "last", "rank")
    assert options["-fa"].allowed_values == ("on", "off", "auto")
    assert options["--flash-attn"].allowed_values == ("on", "off", "auto")
    # 普通占位符（N / TYPE / PATH）不是取值声明，别把它们当成取值
    assert options["-t"].allowed_values == ()
    assert options["-ctk"].allowed_values == ("f32", "f16", "bf16", "q8_0", "q4_0")


def test_deprecated_is_flagged():
    options = parse_help(HELP_SAMPLE)
    assert options["-dt"].deprecated is True
    assert options["-ctk"].deprecated is False


def test_all_aliases_share_one_entry():
    options = parse_help(HELP_SAMPLE)
    assert options["-ngl"] is options["--gpu-layers"] is options["--n-gpu-layers"]
    assert options["-ctkd"] is options["--cache-type-k-draft"]


def test_option_without_argument_keeps_full_description():
    options = parse_help(HELP_SAMPLE)
    info = options["-cl"]
    assert info.arg_hint is None
    assert info.description == "show list of models in cache"


def test_argument_hint_is_split_from_description_by_two_spaces():
    options = parse_help(HELP_SAMPLE)
    assert options["-t"].arg_hint == "N"
    assert options["-ctk"].arg_hint == "TYPE"
    assert options["-fa"].arg_hint == "[on|off|auto]"
    assert options["--pooling"].arg_hint == "{none,mean,cls,last,rank}"
    # 不收参数的选项没有占位符
    assert options["--version"].arg_hint is None


# ========== --spec-type 取值（这条就是原 Bug 的源头） ==========


def test_spec_types_parsed_from_the_option_line():
    values = parse_spec_types(HELP_SAMPLE)
    options = parse_help(HELP_SAMPLE)

    assert values == (
        "none",
        "draft-simple",
        "draft-eagle3",
        "draft-mtp",
        "draft-dflash",
        "ngram-simple",
        "ngram-map-k",
        "ngram-map-k4v",
        "ngram-mod",
        "ngram-cache",
    )
    # schema 里硬编码的那个值**不是合法取值**
    assert "draft-ngram" not in values
    assert options["--spec-type"].default == "none"


def test_spec_types_survive_line_wrapping():
    """取值被折行时只读第一行会**静默漏掉一半**，这条守住它。"""
    text = (
        "--spec-type none,draft-simple,draft-eagle3,draft-mtp,draft-dflash,\n"
        "                                        ngram-simple,ngram-map-k,ngram-mod\n"
        "                                        comma-separated list of types (default: none)\n"
    )
    block = split_blocks(text)[0]
    assert extract_value_list(block, "--spec-type") == (
        "none",
        "draft-simple",
        "draft-eagle3",
        "draft-mtp",
        "draft-dflash",
        "ngram-simple",
        "ngram-map-k",
        "ngram-mod",
    )


# ========== --version ==========


def test_parse_version():
    version, build = parse_version(
        "version: 0 (unknown)\nbuilt with MSVC 19.51.36248.0 for Windows AMD64\n"
    )
    assert version == "0 (unknown)"
    assert build == "MSVC 19.51.36248.0 for Windows AMD64"


def test_parse_version_missing_fields():
    assert parse_version("") == (None, None)


# ========== ServerProbe 查询接口 ==========


def _probe_from(text: str) -> ServerProbe:
    return ServerProbe(path="fake", probed=True, options=parse_help(text))


def test_supports_and_unsupported():
    probe = _probe_from(HELP_SAMPLE)
    assert probe.supports("-ctk") is True
    assert probe.supports("--cache-type-k") is True
    assert probe.supports("--does-not-exist") is False
    assert probe.unsupported(["-ctk", "--nope", "-fa"]) == ["--nope"]
    # 非选项参数不该被当成"不支持"
    assert probe.unsupported(["model.gguf", "8080"]) == []


def test_default_of_returns_none_for_unknown_option():
    probe = _probe_from(HELP_SAMPLE)
    assert probe.default_of("-ctk") == "f16"
    assert probe.default_of("--nope") is None


# ========== 失败路径：永不抛异常 ==========


def test_probe_server_missing_path_returns_error_not_exception():
    for bad in ("", "D:/definitely/not/here/llama-server.exe", None):
        result = probe_server(bad)
        assert result.probed is False
        assert result.error
        assert result.options == {}


def test_probe_server_on_a_file_that_is_not_an_executable(tmp_path):
    """文件存在但不能执行 —— 也要优雅降级，不能把异常抛给接口层。"""
    fake = tmp_path / "not-an-exe.exe"
    fake.write_text("nope", encoding="utf-8")
    result = probe_server(fake, timeout=5.0)
    assert result.probed is False
    assert result.error


# ========== 探测缓存语义 ==========


def _probe_exe(tmp_path):
    exe = tmp_path / "llama-server.exe"
    exe.write_bytes(b"fake")
    return exe


def test_failed_probe_is_cached_for_a_short_ttl(tmp_path, monkeypatch):
    """失败探测结果必须进缓存（短 TTL），否则每个请求都放大两次子进程。

    失败不缓存时，"路径写错/未配置/卡死超时"这类常态失败会把
    to_thread 线程池占满。TTL 到期后才允许重新探测（用户可能刚修好路径）。
    """
    from core import server_probe as sp

    exe = _probe_exe(tmp_path)
    calls = []

    def failing_probe(path, **options):
        calls.append(path)
        return ServerProbe(path=str(path), probed=False, error="boom")

    monkeypatch.setattr(sp, "probe_server", failing_probe)
    sp.clear_server_cache()

    first = sp.probe_server_cached(exe)
    second = sp.probe_server_cached(exe)

    assert first.probed is False and first.error == "boom"
    assert second.probed is False and second.error == "boom"
    assert len(calls) == 1, f"失败结果没有被缓存：探测了 {len(calls)} 次"

    # TTL 到期 → 允许重新探测
    monkeypatch.setattr(sp, "_FAILED_TTL_SECONDS", -1.0)
    sp.probe_server_cached(exe)
    assert len(calls) == 2, "TTL 到期后应重新探测"

    sp.clear_server_cache()


def test_failed_cache_never_leaks_into_the_preview_path(tmp_path, monkeypatch):
    """`probe_server_cached_only` 仍只认成功结果：失败缓存不得改变预览路径语义。

    预览路径（build_argv）的契约是"绝不起子进程、没有就降级"；让它的返回值
    随失败缓存变化会悄悄改变调用方的判断。
    """
    from core import server_probe as sp

    exe = _probe_exe(tmp_path)
    monkeypatch.setattr(
        sp,
        "probe_server",
        lambda path, **options: ServerProbe(path=str(path), probed=False, error="boom"),
    )
    sp.clear_server_cache()

    sp.probe_server_cached(exe)          # 失败结果入短 TTL 缓存
    assert sp.probe_server_cached_only(exe) is None

    sp.clear_server_cache()


def _scan_h1_pids(marker: str) -> list[int]:
    import psutil

    out = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        cmd = " ".join(proc.info["cmdline"] or [])
        if marker in cmd and "h1_holder.py" in cmd:
            out.append(proc.info["pid"])
    return out


def _kill_h1_marked(marker: str) -> None:
    import time as _time

    import psutil

    for pid in _scan_h1_pids(marker):
        try:
            proc = psutil.Process(pid)
            for child in proc.children(recursive=True):
                try:
                    child.kill()
                except psutil.Error:
                    pass
            proc.kill()
        except psutil.Error:
            pass
    _time.sleep(0.5)


def test_probe_child_process_never_inherits_the_session_token(tmp_path, monkeypatch):
    """能力探测也是"把第三方构建跑起来"：同样不得继承 LUNARSHROUD_*。

    外壳把管理令牌经环境注入后端；探测用用户配置的任意路径起 ``--help``。
    旧实现 Popen 不传 ``env=``，令牌原样进入被测进程 —— 拿到它就能
    ``PUT /api/system/config`` 改写 llama_server_path，把一次执行升级成跨启动
    持久化（AGENTS §2.4）。process_manager 的启动路径早有过滤，探测路径没有。
    """
    import os

    from core import server_probe as sp

    exe = _probe_exe(tmp_path)
    monkeypatch.setenv("LUNARSHROUD_SESSION_TOKEN", "t" * 64)
    monkeypatch.setenv("LUNARSHROUD_SESSION_ID", "sess")
    seen = {}

    class FakeProcess:
        def communicate(self, timeout=None):
            return b"--help ok", b""

    def fake_popen(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(sp.subprocess, "Popen", fake_popen)
    sp._run(str(exe), ["--help"], 5.0)

    assert "env" in seen["kwargs"], "探测子进程必须显式传过滤后的 env"
    env = seen["kwargs"]["env"]
    assert not [k for k in env if k.upper().startswith("LUNARSHROUD_")], env
    # 其余环境必须保留：包装脚本/构建要用 PATH 找运行时 DLL
    assert env["PATH"] == os.environ["PATH"]


def test_probe_timeout_survives_a_pipe_holding_wrapper(tmp_path):
    """包装脚本卡死时，探测必须在超时上界附近返回并回收进程树。

    `subprocess.run(timeout=)` 只杀直接子进程；孙进程握着继承的管道会让
    communicate() 无限等（实测挂起 585s）。测试跑在守护线程里，超界判"挂起"。
    """
    import os
    import threading
    import time as _time

    import pytest

    if os.name != "nt":
        pytest.skip("Windows 包装脚本场景")

    marker = tmp_path.name
    holder = tmp_path / "h1_holder.py"
    holder.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    bat = tmp_path / "llama-server.bat"
    bat.write_text(f'@"{sys.executable}" "{holder}"\n', encoding="utf-8")

    box = {}

    def run_probe():
        t0 = _time.monotonic()
        box["result"] = probe_server(bat, timeout=3.0)
        box["elapsed"] = _time.monotonic() - t0

    try:
        worker = threading.Thread(target=run_probe, daemon=True)
        worker.start()
        worker.join(timeout=3.0 + 12.0)
        assert not worker.is_alive(), "探测在超时后仍挂起：孙进程持管道"
        assert box["result"].probed is False
        assert box["elapsed"] < 3.0 + 10, box["elapsed"]
        _time.sleep(0.5)
        leftovers = _scan_h1_pids(marker)
        assert leftovers == [], f"探测进程树未回收: {leftovers}"
    finally:
        _kill_h1_marked(marker)


def test_follower_returns_after_its_deadline_when_the_leader_hangs(tmp_path, monkeypatch):
    """领先者卡死时，跟随者必须在总截止内返回（否则线程泄漏）。

    没有截止的 `while True: wait(1.0)` 会让每个 to_thread 请求泄漏一个永不
    返回的工作线程，最终饿死实例管理 API。以 1s 名义超时驱动：
    跟随预算 = 2×1+5 = 7s。
    """
    import threading
    import time as _time

    from core import server_probe as sp

    exe = _probe_exe(tmp_path)
    started = threading.Event()
    release = threading.Event()

    def hanging_probe(path, **options):
        started.set()
        release.wait(timeout=30)
        return ServerProbe(path=str(path), probed=False, error="late")

    monkeypatch.setattr(sp, "probe_server", hanging_probe)
    sp.clear_server_cache()

    leader_thread = threading.Thread(
        target=lambda: sp.probe_server_cached(exe, timeout=1.0), daemon=True
    )
    leader_thread.start()
    assert started.wait(timeout=5)

    result_box = []
    t0 = _time.monotonic()
    follower = threading.Thread(
        target=lambda: result_box.append(sp.probe_server_cached(exe, timeout=1.0)),
        daemon=True,
    )
    follower.start()
    follower.join(timeout=7.0 + 4.0)
    elapsed = _time.monotonic() - t0
    try:
        assert not follower.is_alive(), "跟随者未按截止返回：线程泄漏"
        assert result_box and result_box[0].probed is False
        assert elapsed < 7.0 + 2.0, elapsed
    finally:
        release.set()
        leader_thread.join(timeout=5)
        sp.clear_server_cache()


def test_excess_followers_fail_fast_instead_of_stacking_workers(tmp_path, monkeypatch):
    """同一目标并发等待者超过上限时快速失败，不再堆占 to_thread 工作线程（复核 N1）。

    冷探测窗口内（领先者挂起、跟随者最长等 2×timeout+5s），无上限的等待会把
    默认线程池（约 32）逐一占满、令实例管理端点排队；这里钉住"超出上限立即
    返回带明确文案的失败"，限额内的等待语义保持原样（仍拿到领先者的真实结果）。
    """
    import threading
    import time as _time

    from core import server_probe as sp

    exe = _probe_exe(tmp_path)
    started = threading.Event()
    release = threading.Event()

    def hanging_probe(path, **options):
        started.set()
        release.wait(timeout=30)
        return ServerProbe(path=str(path), probed=False, error="late")

    monkeypatch.setattr(sp, "probe_server", hanging_probe)
    monkeypatch.setattr(sp, "_MAX_FOLLOWERS_PER_KEY", 2, raising=False)
    sp.clear_server_cache()

    leader_thread = threading.Thread(
        target=lambda: sp.probe_server_cached(exe, timeout=1.0), daemon=True
    )
    leader_thread.start()
    assert started.wait(timeout=5)

    results = []
    lock = threading.Lock()

    def caller(tag):
        r = sp.probe_server_cached(exe, timeout=1.0)
        with lock:
            results.append((tag, r.probed, r.error or ""))

    waiters = [threading.Thread(target=caller, args=(f"w{i}",), daemon=True) for i in range(2)]
    overs = [threading.Thread(target=caller, args=(f"o{i}",), daemon=True) for i in range(3)]
    try:
        for t in waiters:
            t.start()
        _time.sleep(0.3)  # 让限额内的两个先入座
        t0 = _time.monotonic()
        for t in overs:
            t.start()
        for t in overs:
            t.join(timeout=3)
        assert all(not t.is_alive() for t in overs), "超额跟随者没有快速返回（复核 N1）"
        assert _time.monotonic() - t0 < 3.0

        over_results = [r for r in results if r[0].startswith("o")]
        assert len(over_results) == 3
        for _, probed, err in over_results:
            assert probed is False and "等待队列已满" in err, over_results
    finally:
        release.set()
        for t in waiters:
            t.join(timeout=8)
        leader_thread.join(timeout=5)

    waiter_results = [r for r in results if r[0].startswith("w")]
    assert len(waiter_results) == 2, waiter_results
    for _, probed, err in waiter_results:
        assert probed is False and err == "late", waiter_results

    sp.clear_server_cache()


def test_concurrent_cold_probes_share_a_single_execution(tmp_path, monkeypatch):
    """并发冷探测必须共享一次执行（单飞），而不是每线程各起两个子进程（F10b/R2）。"""
    import threading
    import time as _time

    from core import server_probe as sp

    exe = _probe_exe(tmp_path)
    calls = []

    def slow_probe(path, **options):
        calls.append(path)
        _time.sleep(0.4)
        return ServerProbe(path=str(path), probed=True, options={})

    monkeypatch.setattr(sp, "probe_server", slow_probe)
    sp.clear_server_cache()

    results = []
    failures = []
    barrier = threading.Barrier(4)

    def worker():
        try:
            barrier.wait(timeout=5)
            results.append(sp.probe_server_cached(exe))
        except BaseException as exc:  # pragma: no cover - 失败时用于报错
            failures.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not failures, failures
    assert len(results) == 4
    assert all(r.probed for r in results)
    assert len(calls) == 1, f"并发探测没有单飞：executed {len(calls)} 次"

    sp.clear_server_cache()
