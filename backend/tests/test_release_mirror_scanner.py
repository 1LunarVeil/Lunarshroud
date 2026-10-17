"""隐私扫描器（tools/check_release_mirror.py::privacy_hits）的绕过回归测试。

审计实证了 5 种绕过：UTF-16LE、UTF-16BE、gzip、latin-1（一个非法字节即让整份
文件隐身）、JSON 转义的用户目录路径。前一种早已由 test_release_mirror_gate.py
覆盖，其余四种与新增的 fail-closed 行为、工作树侧扫描在这里钉死。

本机标记不写死：门禁从环境变量推导，测试用中性 fixture 标记注入，
这样公开仓库里既没有维护者的用户名/工作区名，测试也不依赖本机路径。
"""
from __future__ import annotations

import gzip
import sys
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import check_release_mirror as gate  # noqa: E402

BS = chr(92)                       # 反斜杠，拼接用，避免源码里出现可匹配的形态
_WORKDIR = "fixture" + "_workspace"
_USERPROFILE = "C:" + BS * 2 + "Users" + BS * 2 + "admin" + BS * 2 + "models"


@pytest.fixture(autouse=True)
def _privacy_marker(monkeypatch):
    """把"本机工作区标记"注入门禁；公开源码里不出现真实工作区名。"""
    monkeypatch.setenv("LUNARSHROUD_PRIVACY_WORKDIR", _WORKDIR)


# ========== 编码绕过 ==========


def test_single_non_utf8_byte_no_longer_hides_the_whole_file(tmp_path):
    """latin-1 / GBK 文本：一个非法字节曾让整份文件的 ASCII 路径全部隐身。"""
    body = ("model_dir=D:/" + _WORKDIR + "/m").encode("ascii")
    (tmp_path / "notes.txt").write_bytes(b"\xc4\xe3\xba\xc3" + body)   # GBK 的"你好"
    hits = gate.privacy_hits(tmp_path, ["notes.txt"])
    assert hits and hits[0][1] == "workdir", hits


def test_utf16be_is_scanned(tmp_path):
    (tmp_path / "be.txt").write_bytes(("D:/" + _WORKDIR).encode("utf-16-be"))
    hits = gate.privacy_hits(tmp_path, ["be.txt"])
    assert hits and hits[0][1] == "workdir", hits


def test_utf16_with_bom_is_scanned(tmp_path):
    """带 BOM 的 UTF-16LE：比裸 UTF-16LE 更常见的落盘形态。"""
    (tmp_path / "bom.txt").write_bytes(("D:/" + _WORKDIR).encode("utf-16"))
    hits = gate.privacy_hits(tmp_path, ["bom.txt"])
    assert hits and hits[0][1] == "workdir", hits


def test_json_escaped_user_profile_is_scanned(tmp_path):
    """JSON 里的绝对路径是转义过的（双反斜杠），单分隔符正则会漏。"""
    (tmp_path / "cfg.json").write_text(
        '{"model_dir":"' + _USERPROFILE + '"}', encoding="utf-8"
    )
    hits = gate.privacy_hits(tmp_path, ["cfg.json"])
    assert hits and hits[0][1] == "userprofile", hits


def test_plain_user_profile_still_scanned(tmp_path):
    """反向保护：放宽到 {1,2} 个分隔符之后，单反斜杠的常规写法不能漏。"""
    (tmp_path / "cfg.txt").write_text(
        "path=C:" + BS + "Users" + BS + "bob" + BS + "x", encoding="utf-8"
    )
    hits = gate.privacy_hits(tmp_path, ["cfg.txt"])
    assert hits and hits[0][1] == "userprofile", hits


# ========== 压缩容器 ==========


@pytest.mark.parametrize(
    ("name", "compress"),
    [("a.gz", gzip.compress), ("b.zlib", zlib.compress)],
)
def test_compressed_containers_are_seen_through(tmp_path, name, compress):
    """压缩不是隐身衣：规则表漏一个归档后缀时，内容扫描是最后一道。"""
    payload = ("path=D:/" + _WORKDIR + "/x").encode("utf-8")
    (tmp_path / name).write_bytes(compress(payload))
    hits = gate.privacy_hits(tmp_path, [name])
    assert hits and hits[0][1] == "workdir", hits


# ========== fail closed ==========


def test_unreadable_file_is_reported_not_skipped(tmp_path):
    """扫不到 == 不能证明干净，所以按命中报出。"""
    hits = gate.privacy_hits(tmp_path, ["does-not-exist.bin"])
    assert hits and hits[0][1] == "unreadable", hits


def test_oversize_file_is_reported_not_skipped(tmp_path, monkeypatch):
    """上限是为了门禁不被拖死，但超限必须留痕，不能静默放行。"""
    (tmp_path / "big.bin").write_bytes(b"x" * 64)
    monkeypatch.setattr(gate, "_MAX_SCAN_BYTES", 16)
    hits = gate.privacy_hits(tmp_path, ["big.bin"])
    assert hits and hits[0][1] == "oversize", hits


# ========== 规则表补齐 ==========


@pytest.mark.parametrize("rel", ["a/b.pdb", "x.lib", "y.exp"])
def test_debug_and_link_artifacts_are_excluded_and_forbidden(rel):
    """.pdb 必然内嵌构建机绝对路径，且是二进制 —— 与 .lnk 事故同一类，一起补齐。"""
    assert gate.is_excluded(rel)[0], f"{rel} 会被 --apply 复制进公开镜像"
    assert gate.is_forbidden(rel), f"{rel} 出现在镜像里必须被判违规"


# ========== 扫描范围 ==========


def test_walk_ignores_git_metadata_on_both_sides(tmp_path):
    """镜像一旦 git init 就有自己的 .git/，门禁必须无视它。

    否则"初始化仓库"这个动作会把发布门禁本身弄坏：上千个 git 对象会被当成
    镜像多出的文件、还要逐个送进隐私扫描，门禁从此永久 FAIL。
    """
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "a.md").write_text("hello" + chr(10), encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]" + chr(10), encoding="utf-8")
    (root / ".git" / "objects").mkdir()
    (root / ".git" / "objects" / "xx").mkdir()
    (root / ".git" / "objects" / "xx" / "obj").write_bytes(bytes(range(256)))

    listing = gate.walk(root, prune_excluded=False)
    assert "docs/a.md" in listing, listing
    assert not [k for k in listing if ".git" in k], (
        f".git 泄漏进了枚举结果：{[k for k in listing if '.git' in k]}"
    )


def test_working_tree_side_is_scanned_before_it_is_published(tmp_path):
    """只扫镜像的话，"工作树已有本机路径、还没 --apply 过去"要等到公开后才发现。

    而 --apply 正是把它公开的那一步。所以工作树待发布侧也必须扫，命中项以
    worktree: 前缀区分，避免与镜像侧重复计数。
    """
    root, mirror = tmp_path / "root", tmp_path / "mirror"
    (root / "docs").mkdir(parents=True)
    mirror.mkdir()
    (root / "docs" / "note.md").write_text(
        "build under D:/" + _WORKDIR, encoding="utf-8"
    )

    report = gate.inspect(root, mirror)
    labels = [hit[0] for hit in report["privacy"]]
    assert "worktree:docs/note.md" in labels, labels
    # 镜像里没有这个文件，因此不应再重复报一份不带前缀的
    assert "docs/note.md" not in labels, labels


def test_both_sides_reported_once_when_the_leak_is_already_mirrored(tmp_path):
    """已同步到镜像的泄露只报一次（镜像侧），不重复计工作树侧。"""
    root, mirror = tmp_path / "root", tmp_path / "mirror"
    (root / "docs").mkdir(parents=True)
    (mirror / "docs").mkdir(parents=True)
    body = "build under D:/" + _WORKDIR
    (root / "docs" / "note.md").write_text(body, encoding="utf-8")
    (mirror / "docs" / "note.md").write_text(body, encoding="utf-8")

    labels = [hit[0] for hit in gate.inspect(root, mirror)["privacy"]]
    assert labels == ["docs/note.md"], labels
