"""展示层格式化工具。

为什么单独一个模块
------------------
"人类可读的文件大小"曾同时存在于三处（``model_scanner`` 与 ``github/`` 镜像
里的 ``_format_size``、``gguf_probe`` 里的 ``format_size``），而且 MB 精度
还不一致（``5.0 MB`` vs ``5.00 MB``）—— 同一个文件会因为走哪条代码路径
显示得不一样。

这类"同一个事实两份实现"是必须消掉的：它不会立刻报错，只会在某天让人
对着两个不同的数字发呆。放在这里，谁都可以 import，也不会形成环。
"""
from __future__ import annotations


def format_size(size_bytes: int) -> str:
    """把字节数转成人类可读大小。"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024**2:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024**3:
        return f"{size_bytes / 1024**2:.2f} MB"
    return f"{size_bytes / 1024**3:.2f} GB"
