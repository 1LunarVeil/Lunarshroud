"""探测 URL 的本机边界校验。

**为什么单独一个模块**：健康探测的 URL 是拼出来的（宿主常量 + 端口），
静态分析器无法从调用点看出"目标必然是本机"。把校验写成独立、可单测的函数，
调用方在请求前显式过一遍，安全属性就从注释变成了代码。

`_probe_health` 只探自己刚启动的子进程，但这个前提值得在执行处被证明一次：
主机名解析后必须落在回环网段，协议必须是 http。
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit


def refuse_non_loopback(url: str) -> str:
    """为一个本机探测 URL 做边界校验；可以请求时返回空串，否则返回原因。

    三条判据：

    1. 协议必须是 http —— 不接受 file:// / ftp:// 这类奇怪协议；
    2. 主机名解析之后必须**全部**落在回环网段（``ipaddress.is_loopback``）；
    3. 至少解析出一个地址。

    第 2 条是关键：只比对字面量 "127.0.0.1" 会被 ``localhost`` 的 hosts 覆盖
    绕过。这里解析真实地址再判断，所以"名字看着像本机、实际指向别处"的组合
    会被拦下（DNS rebinding 的那一类）。

    返回的是人话原因，调用方写进 ``instance._last_probe``，让启动失败的排查
    能直接看到"探测被拒绝"，而不是一句莫名的超时。
    """
    try:
        parts = urlsplit(url)
    except ValueError as error:
        return f"URL 无法解析: {error}"

    if parts.scheme != "http":
        return f"只允许 http，实际 {parts.scheme!r}"

    host = parts.hostname or ""
    if not host:
        return "URL 没有主机名"

    try:
        infos = socket.getaddrinfo(host, parts.port or 80, proto=socket.IPPROTO_TCP)
    except OSError as error:
        return f"主机名无法解析: {error}"

    resolved: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        address = str(info[4][0])
        # IPv6 的 scope id（`fe80::1%eth0`）不属于地址本身。
        try:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
        except ValueError:
            return f"无法识别的地址: {address}"
        resolved.append(ip)
        if not ip.is_loopback:
            return f"{host} 解析到非回环地址 {ip}"

    if not resolved:
        return "主机名没有解析出任何地址"
    return ""
