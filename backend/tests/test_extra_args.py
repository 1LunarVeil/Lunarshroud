"""附加命令行参数（`extra_args`）：面板放常用项，长尾交给用户手填。

llama.cpp 有 400+ 个选项，穷举进面板既做不完也没人找得到。所以给一个
"原样追加"的自由文本参数：拆成 argv 片段、落在命令**末尾**，因此还能覆盖
上面面板写出来的同名参数。

这份测试盯住三件容易出事的地方：
1. 拆分规则（Windows 路径的反斜杠不能被当转义符吃掉）
2. 引号没闭合必须**报错**，不能静默拆成残缺命令
3. 密钥脱敏在"自由文本"这条新通道上不能漏（`--api-key=xxx` 是**一个** token）
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from core.param_builder import (  # noqa: E402
    build_argv,
    build_command_string,
    redact_argv,
    scrub_sensitive_params,
    split_cli_snippet,
)
from core.param_schema import PARAM_DEFINITIONS, get_default_params  # noqa: E402

FAKE_EXE = "llama-server.exe"


def base_params(**overrides) -> dict:
    return {**get_default_params(), "model": "D:/m.gguf", **overrides}


# ========== 拆分规则 ==========


def test_splits_on_whitespace():
    assert split_cli_snippet("--cache-prompt --cont-batching") == ["--cache-prompt", "--cont-batching"]
    assert split_cli_snippet("  --a   b  ") == ["--a", "b"]
    assert split_cli_snippet("") == []


def test_quotes_keep_spaces_together():
    assert split_cli_snippet('--chat-template-file "C:/my tpl/a.jinja"') == [
        "--chat-template-file", "C:/my tpl/a.jinja"
    ]
    assert split_cli_snippet("--flag 'a b'") == ["--flag", "a b"]


def test_backslashes_are_literal_not_escapes():
    """Windows 路径必须原样保留 —— 用 shlex 的 POSIX 模式会把 \\a 之类吃掉。"""
    assert split_cli_snippet(r'--log-file "D:\logs\llama.log"') == [
        "--log-file", r"D:\logs\llama.log"
    ]
    assert split_cli_snippet(r"--path C:\models\webui") == ["--path", r"C:\models\webui"]


def test_unclosed_quote_is_an_error():
    with pytest.raises(ValueError, match="引号没有闭合"):
        split_cli_snippet('--flag "unterminated')


# ========== 进命令行 ==========


def test_extra_args_land_at_the_very_end():
    argv = build_argv(base_params(extra_args="--cache-prompt --cont-batching"), FAKE_EXE)
    assert argv[-2:] == ["--cache-prompt", "--cont-batching"]


def test_extra_args_can_override_earlier_flags():
    """排最后是它的语义：手填的值要能盖住面板里设的。"""
    argv = build_argv(base_params(ctx_size=8192, extra_args="-c 4096"), FAKE_EXE)
    assert argv.index("-c") < argv.index("-c", argv.index("-c") + 1)  # 两个 -c
    assert argv[-1] == "4096"


def test_empty_extra_args_change_nothing():
    plain = build_argv(base_params(), FAKE_EXE)
    for empty in ("", "   "):
        assert build_argv(base_params(extra_args=empty), FAKE_EXE) == plain


def test_extra_args_are_the_last_parameter_in_the_schema():
    """顺序即语义 —— 以后往 schema 末尾加参数时，这条会挡住"不小心插到它后面"。"""
    assert PARAM_DEFINITIONS[-1].key == "extra_args"
    assert PARAM_DEFINITIONS[-1].append_raw is True


# ========== 脱敏（新通道不能漏） ==========


def test_api_key_is_redacted_in_both_spellings():
    """`--api-key=xxx` 在自由文本里是**一个** token，相邻判断抓不到它。"""
    argv = [FAKE_EXE, "--api-key", "secret", "--api-key=also-secret", "--host", "127.0.0.1"]
    redacted = redact_argv(argv)
    assert "secret" not in redacted
    assert "also-secret" not in redacted
    assert redacted[2] == "[REDACTED]"
    assert redacted[3] == "--api-key=[REDACTED]"


def test_command_string_redacts_equals_form():
    text = build_command_string([FAKE_EXE, "--api-key=secret", "-c", "8192"])
    assert "secret" not in text
    assert "[REDACTED]" in text


# ========== 启动前校验 ==========


def test_launch_validation_rejects_unclosed_quote(tmp_path):
    from core.process_manager import ProcessManager

    exe = tmp_path / "llama-server.exe"
    model = tmp_path / "model.gguf"
    exe.write_bytes(b"exe")
    model.write_bytes(b"model")

    manager = ProcessManager()
    with pytest.raises(ValueError, match="引号没有闭合"):
        manager._validate_start(
            {"model": str(model), "extra_args": '--flag "oops'}, str(exe)
        )


def test_launch_validation_caps_the_token_count(tmp_path):
    from core.process_manager import MAX_EXTRA_ARG_TOKENS, ProcessManager

    exe = tmp_path / "llama-server.exe"
    model = tmp_path / "model.gguf"
    exe.write_bytes(b"exe")
    model.write_bytes(b"model")

    manager = ProcessManager()
    too_many = " ".join(f"--opt{i}" for i in range(MAX_EXTRA_ARG_TOKENS + 1))
    with pytest.raises(ValueError, match="too many extra arguments"):
        manager._validate_start({"model": str(model), "extra_args": too_many}, str(exe))


def test_launch_validation_accepts_a_normal_snippet(tmp_path):
    from core.process_manager import ProcessManager

    exe = tmp_path / "llama-server.exe"
    model = tmp_path / "model.gguf"
    exe.write_bytes(b"exe")
    model.write_bytes(b"model")

    manager = ProcessManager()
    manager._validate_start(
        {"model": str(model), "extra_args": '--cache-prompt --mmproj "D:\\my mmproj\\f16.gguf"'},
        str(exe),
    )


def test_launch_validation_rejects_extra_args_overriding_managed_flags(tmp_path):
    """extra_args 不得覆盖启动器据以做安全/身份判定的 flag。

    extra_args 追加在命令行末尾、能覆盖面板写出的同名参数，而这些校验读的是
    params[...]。两者脱节就等于没有边界：`--host 0.0.0.0` 曾可绕过"非回环必须
    配 api_key"。两种拼写（`--x v` 与 `--x=v`）都要挡住。
    """
    from core.process_manager import ProcessManager

    exe = tmp_path / "llama-server.exe"
    model = tmp_path / "model.gguf"
    exe.write_bytes(b"exe")
    model.write_bytes(b"model")
    manager = ProcessManager()

    for snippet in (
        "--host 0.0.0.0",
        "--host=0.0.0.0",
        "--port 9999",
        "--api-key leaked",
        "--path C:\\Users",
        "--log-file C:\\Windows\\Temp\\x.log",
        "--cache-prompt --host 0.0.0.0",   # 混在合法片段里也要被抓出来
    ):
        with pytest.raises(ValueError, match="launcher-managed flags"):
            manager._validate_start(
                {"model": str(model), "host": "127.0.0.1", "extra_args": snippet},
                str(exe),
            )


def test_effective_host_check_holds_even_if_the_blacklist_is_relaxed(tmp_path):
    """承重墙是"按 argv 上最终生效的值判定"，不是黑名单本身。

    黑名单给的是清晰的错误信息；这一条保证即使将来漏掉某个拼写或放宽了黑名单，
    只要命令行上最后一次 --host 不是回环，就仍然要求 api_key。
    """
    from core.param_builder import effective_host_and_key

    assert effective_host_and_key({"host": "127.0.0.1"}, ["--host", "0.0.0.0"]) == ("0.0.0.0", "")
    assert effective_host_and_key({"host": "127.0.0.1"}, ["--host=0.0.0.0"]) == ("0.0.0.0", "")
    # api_key 同样按最终生效值取
    assert effective_host_and_key(
        {"host": "0.0.0.0", "api_key": ""}, ["--api-key=k"]
    ) == ("0.0.0.0", "k")
    # 没有 extra_args 时退回 params 声明值；host 缺失时按 schema 默认
    assert effective_host_and_key({}, []) == ("", "")
    assert effective_host_and_key({"host": "127.0.0.1", "api_key": "k"}, []) == ("127.0.0.1", "k")


def test_preset_import_rejects_extra_args_overriding_managed_flags():
    """预设是 SECURITY_MODEL 明列的不可信输入，必须在**入库前**就挡下。

    只靠 _validate_start 的话，用户会先看到"导入成功"，直到点启动才报错 ——
    而那条载荷已经躺在 data/presets/*.json 里了。
    """
    from core.preset_store import _validate_preset_data

    errors = _validate_preset_data(
        {
            "name": "innocent looking",
            "params": {
                "model": "C:\\models\\a.gguf",
                "host": "127.0.0.1",
                "extra_args": "--host 0.0.0.0",
            },
        },
        allow_empty_model=True,
    )
    assert any("launcher-managed flags" in e for e in errors), errors


@pytest.mark.parametrize(
    ("snippet", "expected"),
    [
        ("-m D:/models/B.gguf", "-m"),
        ("--model D:/models/B.gguf", "--model"),
        ("--model-url http://evil.example/m.gguf", "--model-url"),
        ("-mu http://evil.example/m.gguf", "-mu"),
        ("--cors-origins *", "--cors-origins"),
        ("--cors-origins=localhost", "--cors-origins"),
    ],
)
def test_extra_args_cannot_change_which_model_loads_or_the_cors_policy(
    tmp_path, snippet, expected
):
    """extra_args 不能改写"实际加载哪个模型"和"默认 CORS 策略"。

    ``--host`` / ``--api-key`` 早就进了黑名单，机制却对身份类参数一样成立：
    ``extra_args`` 追加在 argv 末尾，而 llama-server 对重复的单值参数**取最后一
    个**（本机实测 ``-m A -m B`` 先打 ``DEPRECATED: argument '-m' specified
    multiple times``，随后加载 B）。预检、``_validate_start`` 的模型存在性检查、
    历史记录、显存预估读的全是 ``params["model"]`` —— 于是校验通过、界面显示 A、
    跑的却是从没被校验过的 B。

    ``--cors-origins`` 是同一缺口的另一面：本机 ``--help`` 写着
    ``--cors-origins ORIGINS (default: *)``，面板特意收紧成 localhost；末尾补一条
    ``--cors-origins *`` 就退回"任何网站都能跨源读这个本地模型 API"，而这条边界
    正是 SECURITY_MODEL 自己声明要避免的默认风险。

    黑名单的取值范围跟着 ``config_resolver.MANAGED_KEYS`` 走（model / host /
    port / api_key / cors_origins / path / log_file），不单方面扩大 —— 所以
    ``--mmproj`` 仍然允许出现在附加参数里（见上面那条 normal-snippet 用例）。
    """
    from core.param_builder import find_forbidden_extra_args, split_cli_snippet
    from core.preset_store import _validate_preset_data
    from core.process_manager import ProcessManager

    # 命中时返回的是**原始片段**（`--cors-origins=localhost` 整条），
    # 这样报错信息对用户才有意义；判定用的是 partition("=")[0]。
    hits = find_forbidden_extra_args(split_cli_snippet(snippet))
    assert hits and hits[0].partition("=")[0] == expected, hits

    params = {"model": "D:/models/A.gguf", "host": "127.0.0.1", "extra_args": snippet}
    errors = _validate_preset_data({"name": "p", "params": params})
    assert any(expected in e for e in errors), f"导入放过了 {snippet}: {errors}"

    exe = tmp_path / "llama-server.exe"
    exe.write_bytes(b"exe")
    (tmp_path / "A.gguf").write_bytes(b"model")
    manager = ProcessManager()
    with pytest.raises(ValueError, match="launcher-managed flags"):
        manager._validate_start(params, str(exe))


def test_managed_flag_blacklist_covers_every_schema_spelling():
    """黑名单必须含受管控参数在 schema 里声明的那种拼写，否则等于没拦。

    面板写的是 ``-m``（不是 ``--model``）。只列长拼写的话，用户从面板里能正常
    启动，而攻击性预设换成短拼写就绕过了 —— 这类漂移要靠派生而不是手工清单防住。
    """
    from core.param_builder import (
        SECURITY_CONTROLLED_PARAM_KEYS,
        security_controlled_flags,
    )

    flags = security_controlled_flags()
    by_key = {meta.key: meta for meta in PARAM_DEFINITIONS}
    for key in SECURITY_CONTROLLED_PARAM_KEYS:
        meta = by_key.get(key)
        if meta and meta.cli:
            assert meta.cli in flags, f"{key} 的 {meta.cli} 没进黑名单"
    assert "--cors-origins" in flags and "-m" in flags


# ========== schema 侧的暴露 ==========


def test_extra_args_is_exposed_to_the_frontend_as_a_plain_text_field():
    """前端不需要为它写任何特殊分支：type=text + cli=None 就会渲染成输入框。"""
    from core.param_schema import get_schema_for_frontend

    param = next(p for p in get_schema_for_frontend()["params"] if p["key"] == "extra_args")
    assert param["type"] == "text"
    assert param["cli"] is None          # 不进 flag 分支
    assert param["label_en"] and param["help_en"]


# ========== 持久化侧的脱敏 ==========


def test_api_key_hidden_in_extra_args_is_scrubbed_before_persistence():
    """脱敏不能只按键名做，否则自由文本里的密钥会原样落盘。

    `scrub_sensitive_params` 曾只清空 params["api_key"]，于是写在 extra_args 里的
    `--api-key=xxx` 会进入 history.db 的 params_json 与 data/presets/*.json，
    并经 /api/presets/{id}/export 作为可下载附件外发。argv 侧的 redact_argv
    一直是好的 —— 漏的恰恰是持久化这一侧。
    """
    out = scrub_sensitive_params({
        "api_key": "TOP-LEVEL-SECRET",
        "extra_args": "--api-key=EMBEDDED-SECRET --alias x",
    })
    assert out["api_key"] == ""
    assert "TOP-LEVEL-SECRET" not in str(out)
    assert "EMBEDDED-SECRET" not in str(out)
    assert out["extra_args"] == "--api-key=[REDACTED] --alias x"


def test_scrub_covers_the_space_separated_spelling_too():
    """`--api-key secret` 与 `--api-key=secret` 两种写法都要盖住。"""
    spaced = scrub_sensitive_params({"extra_args": "--api-key SECRET --alias x"})["extra_args"]
    assert "SECRET" not in spaced
    assert spaced == "--api-key [REDACTED] --alias x"


def test_scrub_leaves_unrelated_params_untouched():
    """脱敏不得误伤：没有密钥形态的自由文本必须原样保留。"""
    params = {"extra_args": "--cache-prompt --cont-batching", "ctx_size": 8192}
    assert scrub_sensitive_params(params) == params
