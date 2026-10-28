from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from core import knowledge_extract


def _docx(path: Path, paragraphs: list[str]) -> None:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)


def test_extract_text_decodes_gbk_fallback_and_keeps_heading_text(tmp_path):
    path = tmp_path / "法条.md"
    path.write_bytes("# 第一章\n\n个人信息处理者应当合法处理。\n".encode("gb18030"))

    document = knowledge_extract.extract_file(path)

    assert "个人信息处理者" in document.text
    assert document.kind == "text"
    assert document.title == "第一章"


def test_extract_html_strips_scripts_styles_and_forms(tmp_path):
    path = tmp_path / "page.html"
    path.write_text(
        "<html><head><title>测试页</title><style>.x{}</style></head>"
        "<body><script>alert(1)</script><form>忽略</form><p>正文<strong>内容</strong></p></body></html>",
        encoding="utf-8",
    )

    document = knowledge_extract.extract_file(path)

    assert document.title == "测试页"
    assert "正文内容" in document.text
    assert "alert" not in document.text
    assert "忽略" not in document.text


def test_extract_docx_uses_standard_library(tmp_path):
    path = tmp_path / "资料.docx"
    _docx(path, ["第一条 总则", "第二条 数据安全"])

    document = knowledge_extract.extract_file(path)

    assert "第一条 总则" in document.text
    assert "第二条 数据安全" in document.text
    assert document.kind == "docx"


def test_extract_rejects_binary_and_unsupported(tmp_path):
    binary = tmp_path / "x.txt"
    binary.write_bytes(b"a\x00b")
    with pytest.raises(ValueError):
        knowledge_extract.extract_file(binary)

    unsupported = tmp_path / "x.exe"
    unsupported.write_bytes(b"MZ")
    with pytest.raises(ValueError):
        knowledge_extract.extract_file(unsupported)


def test_collect_paths_recurses_skips_links_and_caps(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.md").write_text("a", encoding="utf-8")
    sub = root / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("b", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    try:
        (root / "link.txt").symlink_to(outside)
    except OSError:
        pass

    files = knowledge_extract.collect_paths([str(root)], max_files=10)

    assert {p.name for p in files} == {"a.md", "b.txt"}
    assert all(p.is_relative_to(root) for p in files)
    with pytest.raises(ValueError):
        knowledge_extract.collect_paths([str(root)], max_files=1)


def test_collect_paths_refuses_a_named_path_that_does_not_exist(tmp_path):
    """A typo'd file must not be dropped silently (AGENTS §2.24).

    The UI reports "added" from this call's success, so skipping a named
    missing file would leave the user believing the document is indexed.
    """
    real = tmp_path / "real.md"
    real.write_text("real", encoding="utf-8")
    ghost = tmp_path / "ghost.md"

    with pytest.raises(ValueError, match="does not exist"):
        knowledge_extract.collect_paths([str(ghost)])

    # The same name disappearing between listing and stat is a scan race, not a
    # user choice, so directory walks still skip it.
    walk = tmp_path / "walk"
    walk.mkdir()
    doomed = walk / "doomed.md"
    doomed.write_text("x", encoding="utf-8")
    doomed.unlink()
    assert knowledge_extract.collect_paths([str(walk)]) == []


def test_chunk_sections_preserves_page_and_avoids_empty_chunks():
    from core.knowledge_extract import ExtractedSection, chunk_sections

    sections = [
        ExtractedSection(text="一" * 2500, page=3, heading="第三章"),
        ExtractedSection(text="短文本", page=4, heading=None),
    ]
    chunks = chunk_sections(sections, chunk_chars=1000, overlap=100)

    assert len(chunks) == 4
    assert all(chunk.text.strip() for chunk in chunks)
    assert chunks[0].page == 3
    assert chunks[0].heading == "第三章"
    assert chunks[-1].page == 4
    assert all(chunk.char_end > chunk.char_start for chunk in chunks)
    assert all(len(chunk.text) <= 1000 for chunk in chunks)


def test_chunk_sections_label_the_statute_article_in_force():
    """Chunks carry the article they belong to, but are not split on it.

    Measured on the legal KB: splitting chunks on 第X条 boundaries dropped the
    no-rerank hybrid hit@1 from 52% to 35% (small article chunks dilute both
    retrieval arms), while labelling the existing windowed chunks gives the same
    citation clarity with no retrieval cost. Keep the windows; add the label.
    """
    from core.knowledge_extract import ExtractedSection, chunk_sections

    article = "本条用于占位，使每条长度接近分块上限的一半。" * 6
    text = (
        "第一章 总则\n"
        f"第十六条 甲规定。{article}\n"
        f"第十七条 乙规定。{article}\n"
        f"第十八条 丙规定。{article}"
    )
    chunks = chunk_sections([ExtractedSection(text=text, page=2, heading=None)], chunk_chars=200, overlap=0)

    assert chunks
    assert all(len(chunk.text) <= 200 for chunk in chunks), "window size must not change"
    labels = [chunk.heading for chunk in chunks]
    assert "第十六条" in labels
    assert "第十七条" in labels
    assert "第十八条" in labels
    assert all(labels), f"unlabelled chunk: {labels}"

    preamble = chunk_sections(
        [ExtractedSection(text="第一章 总则\n" + "为保护合法权益制定本法。" * 30, page=1, heading=None)],
        chunk_chars=200,
        overlap=0,
    )
    assert preamble[0].heading == "第一章", "a chunk with no article yet should fall back to its chapter"


def test_png_encoder_emits_a_valid_rgb_image():
    import struct
    import zlib

    png = knowledge_extract.encode_png_bgr(b"\x01\x02\x03\x04\x05\x06", 2, 1, 6)

    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    offset = 8
    chunks = {}
    while offset < len(png):
        (length,) = struct.unpack(">I", png[offset:offset + 4])
        kind = png[offset + 4:offset + 8]
        chunks[kind] = png[offset + 8:offset + 8 + length]
        offset += 12 + length
    width, height = struct.unpack(">II", chunks[b"IHDR"][:8])
    assert (width, height) == (2, 1)
    assert zlib.decompress(chunks[b"IDAT"]) == b"\x00\x03\x02\x01\x06\x05\x04"


def test_scanned_pdf_uses_the_ocr_hook(tmp_path, monkeypatch):
    from pypdf import PdfWriter

    path = tmp_path / "scan.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    with path.open("wb") as stream:
        writer.write(stream)

    monkeypatch.setattr(
        knowledge_extract, "render_pdf_page", lambda *args, **kwargs: b"png-bytes"
    )
    calls = []

    def fake_ocr(png: bytes, page: int) -> str:
        calls.append((png, page))
        return "第一条 个人信息处理者应当合法处理。"

    document = knowledge_extract.extract_file(path, ocr=fake_ocr)

    assert calls == [(b"png-bytes", 1)]
    assert document.kind == "pdf-ocr"
    assert "个人信息处理者" in document.text

    with pytest.raises(ValueError, match="need an OCR model"):
        knowledge_extract.extract_file(path)
