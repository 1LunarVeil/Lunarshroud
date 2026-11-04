"""把 PARAM_LABELS_EN / PARAM_HELP_EN 的值内联进各 ParamMeta 定义（一次性脚本）。

为什么用脚本而不是手改
----------------------
92 个参数、每条英文 help 都是长段落。手改必然漏字符，而漏掉的字符在
schema JSON 里才会暴露。这里做机械转换，再用 schema JSON diff 验证"输出完全不变"。

用法（只读分析，不写文件）：
    python tools/migrate_params_to_single_source.py backend/core/param_schema.py
输出一份需要人工套用的补丁片段；真正的写入由 Edit 工具完成（安全策略要求）。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def load_schema_module(path: Path):
    # 以 backend/ 为导入根（模块内有 `from core...` 形式的导入），不能按文件路径裸加载。
    backend_dir = path.resolve().parents[1]
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    from core import param_schema as module

    return module


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    path = Path(argv[1])
    module = load_schema_module(path)
    labels = module.PARAM_LABELS_EN
    helps = module.PARAM_HELP_EN
    source = path.read_text(encoding="utf-8")

    # 统计：哪些 key 在定义里已有 label_en / help_en 字段（重复迁移会报错）
    existing = len(re.findall(r"^\s+label_en\s*=", source, re.M))
    print(f"ParamMeta 定义中已有 label_en 字段: {existing}")
    print(f"PARAM_LABELS_EN 条目: {len(labels)}")
    print(f"PARAM_HELP_EN 条目: {len(helps)}")

    meta_keys = [p.key for p in module.PARAM_DEFINITIONS]
    missing_label = [k for k in meta_keys if k not in labels]
    missing_help = [k for k in meta_keys if k not in helps]
    extra_label = [k for k in labels if k not in meta_keys]
    extra_help = [k for k in helps if k not in meta_keys]
    print(f"定义缺少英文标签: {missing_label}")
    print(f"定义缺少英文帮助: {missing_help}")
    print(f"字典多余条目: {extra_label} / {extra_help}")

    # 输出待内联的数据（供 Edit 精确替换时对照）
    print("\n=== 待内联（key -> label_en / help_en 前 60 字）===")
    for key in meta_keys:
        label = labels.get(key, "")
        help_text = helps.get(key, "")
        print(f"{key}: {json.dumps(label, ensure_ascii=False)} | {json.dumps(help_text[:60], ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
