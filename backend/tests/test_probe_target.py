"""`core.probe_target.refuse_non_loopback` 的边界回归。

健康探测的目标只能是本机，但 URL 是拼出来的（宿主 + 端口）。静态分析器
看不出这一点，所以校验必须是一段**可执行、可单测**的代码，而不是注释。

这些用例钉住的是"拒绝什么"，因为漏检的后果是探测器替调用方对外发请求。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from core.probe_target import refuse_non_loopback  # noqa: E402


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/health",
        "http://127.0.0.2:8080/health",  # 整个 127/8 都是回环
        "http://localhost:8080/health",
        "http://[::1]:8080/health",
    ],
)
def test_loopback_targets_are_allowed(url):
    assert refuse_non_loopback(url) == ""


@pytest.mark.parametrize(
    "url,reason_fragment",
    [
        # 协议不对
        ("https://127.0.0.1:8080/health", "只允许 http"),
        ("file:///etc/passwd", "只允许 http"),
        # 通配地址不是可连接目标
        ("http://0.0.0.0:8080/health", "非回环"),
        # 外部域名/地址必须被解析后拒绝
        ("http://example.com/health", "非回环"),
        ("http://192.168.1.10:8080/health", "非回环"),
        # 云元数据地址（SSRF 的典型目标）
        ("http://169.254.169.254/latest/meta-data/", "非回环"),
    ],
)
def test_non_loopback_targets_are_refused(url, reason_fragment):
    reason = refuse_non_loopback(url)
    assert reason, f"{url} 必须被拒绝"
    assert reason_fragment in reason


def test_a_url_without_a_host_is_refused():
    assert refuse_non_loopback("http:///health")


def test_the_refusal_reason_is_human_readable():
    """原因会写进 instance._last_probe，必须能直接回答"为什么没探通"。"""
    reason = refuse_non_loopback("http://192.168.1.10:8080/health")
    assert "192.168.1.10" in reason
    assert "非回环" in reason
