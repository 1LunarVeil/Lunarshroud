"""测试用的最小 GGUF 写入器。

只写到张量信息为止，不写张量数据段 —— 探测器不读那一段，而这样能精确控制
每个字段（尤其是"超长数组""畸形头部""assistant 架构"）。

放在单独模块里由多个测试文件共用：GGUF 的字节布局是一份**事实**，
在两个测试文件里各写一遍迟早会分叉。
"""
from __future__ import annotations

import struct
from pathlib import Path


def _str(value: str) -> bytes:
    raw = value.encode("utf-8")
    return struct.pack("<Q", len(raw)) + raw


def _kv(key: str, type_code: int, payload: bytes) -> bytes:
    return _str(key) + struct.pack("<I", type_code) + payload


def kv_str(key: str, value: str) -> bytes:
    return _kv(key, 8, _str(value))


def kv_u32(key: str, value: int) -> bytes:
    return _kv(key, 4, struct.pack("<I", value))


def kv_u64(key: str, value: int) -> bytes:
    return _kv(key, 10, struct.pack("<Q", value))


def kv_bool(key: str, value: bool) -> bytes:
    return _kv(key, 7, struct.pack("<?", value))


def kv_f32(key: str, value: float) -> bytes:
    return _kv(key, 6, struct.pack("<f", value))


def kv_str_array(key: str, values: list[str]) -> bytes:
    body = struct.pack("<I", 8) + struct.pack("<Q", len(values))
    body += b"".join(_str(v) for v in values)
    return _kv(key, 9, body)


def kv_f32_array(key: str, values: list[float]) -> bytes:
    body = struct.pack("<I", 6) + struct.pack("<Q", len(values))
    body += b"".join(struct.pack("<f", v) for v in values)
    return _kv(key, 9, body)


def tensor(name: str, dims: tuple[int, ...] = (512, 512), ttype: int = 2, offset: int = 0) -> bytes:
    out = _str(name) + struct.pack("<I", len(dims))
    out += b"".join(struct.pack("<Q", d) for d in dims)
    out += struct.pack("<I", ttype) + struct.pack("<Q", offset)
    return out


def write_gguf(path: Path, *, version: int = 3, kvs=(), tensors=()) -> Path:
    data = b"GGUF"
    data += struct.pack("<I", version)
    data += struct.pack("<Q", len(tensors))
    data += struct.pack("<Q", len(kvs))
    data += b"".join(kvs)
    data += b"".join(tensors)
    path.write_bytes(data)
    return path
