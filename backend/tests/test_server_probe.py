"""llama-server 能力探测测试。

夹具用的是**从真实 ``--help`` 摘录的排版**（含列 0 = 新选项、多字符短选项、
六种 default 变体），而不是我理想中的格式 —— 解析器的难点全在这些坑里。
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


# ========== 默认值解析的六种排版 ==========


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
