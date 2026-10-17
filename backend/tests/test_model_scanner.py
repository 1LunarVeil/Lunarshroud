"""模型扫描：量化标记识别与目录浏览。

_detect_quant 原先零测试覆盖，所以 QUANT_PATTERN 的死分支（裸 Q\\d 排在
Q6_K|Q2_K|Q3_K 之前，把 Q6_K 截成 Q6）长期没人发现。
"""
from __future__ import annotations

import pytest
from core.model_scanner import _detect_quant, list_directory


@pytest.mark.parametrize(
    "marker,expected",
    [
        # 原先被裸 Q\d 截断的四类
        ("Q2_K", "Q2_K"),
        ("Q3_K", "Q3_K"),
        ("Q4_K", "Q4_K"),
        ("Q6_K", "Q6_K"),
        ("Q5_K", "Q5_K"),
        ("Q6_K_L", "Q6_K_L"),
        # 原本就正确的，防止改坏
        ("Q4_K_S", "Q4_K_S"),
        ("Q4_K_M", "Q4_K_M"),
        ("Q5_K_M", "Q5_K_M"),
        ("Q8_0", "Q8_0"),
        ("Q4_0", "Q4_0"),
        ("Q5_1", "Q5_1"),
        ("Q4_1", "Q4_1"),
        ("IQ4_XS", "IQ4_XS"),
        ("IQ2_M", "IQ2_M"),
        ("BF16", "BF16"),
        ("F16", "F16"),
        ("F32", "F32"),
        ("Q2", "Q2"),
        ("Q8", "Q8"),
    ],
)
def test_detect_quant(marker, expected):
    assert _detect_quant(f"model-{marker}.gguf") == expected


def test_detect_quant_is_case_insensitive():
    assert _detect_quant("Model-q6_k.gguf") == "Q6_K"


def test_detect_quant_unknown():
    assert _detect_quant("weights.gguf") == "unknown"


def test_list_directory_separates_dirs_and_gguf(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a-q4_k_s.gguf").write_bytes(b"\x00")
    (tmp_path / "b-q6_k.gguf").write_bytes(b"\x00")
    (tmp_path / "notes.txt").write_text("ignore me", encoding="utf-8")

    items = list_directory(str(tmp_path))

    names = [i["name"] for i in items]
    assert names[0] == "sub", "目录必须排在文件之前"
    assert "notes.txt" not in names, "非 .gguf 文件不该出现"
    quants = {i["name"]: i for i in items if i["type"] == "file"}
    assert quants["b-q6_k.gguf"]["size_bytes"] == 1


def test_list_directory_rejects_missing_and_empty(tmp_path):
    assert list_directory("") == []
    assert list_directory(str(tmp_path / "nope")) == []
    assert list_directory(str(tmp_path)) == []


def test_list_directory_honours_max_items(tmp_path):
    for i in range(5):
        (tmp_path / f"m{i}.gguf").write_bytes(b"\x00")
    assert len(list_directory(str(tmp_path), max_items=2)) == 2


def _make_junction(link, target) -> bool:
    import subprocess

    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def test_scan_models_does_not_descend_into_junctions(tmp_path):
    """junction 不是普通目录：扫描不得穿越（审计 W2 已实证）。"""
    from core.model_scanner import scan_models

    root = tmp_path / "models"
    real = root / "real"
    real.mkdir(parents=True)
    (real / "inside-q4_k_m.gguf").write_bytes(b"\x00")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret-q4_k_m.gguf").write_bytes(b"\x00")

    link = root / "portal"
    if not _make_junction(link, outside):
        pytest.skip("junction creation unavailable")

    found = {m["filename"] for m in scan_models(str(root))}

    assert "inside-q4_k_m.gguf" in found
    assert not any("secret" in name for name in found), (
        "扫描穿越了 junction：根外文件被收录"
    )


def test_list_directory_marks_junctions_as_links_not_dirs(tmp_path):
    """list_directory 把目录形态的链接标成 link，调用方据此不下钻。"""
    root = tmp_path / "models"
    root.mkdir()
    (root / "real").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    link = root / "portal"
    if not _make_junction(link, outside):
        pytest.skip("junction creation unavailable")

    by_name = {item["name"]: item for item in list_directory(str(root))}
    assert by_name["portal"]["type"] == "link"
    assert by_name["real"]["type"] == "dir"
