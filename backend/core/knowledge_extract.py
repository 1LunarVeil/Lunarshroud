"""Local document extraction and chunking for the knowledge base.

The extractor is deliberately local-only and budgeted. PDFs use their text
layer when present and fall back to a caller-supplied OCR hook per scanned
page, DOCX is read through the standard library (a .docx is a zip of XML
parts), and HTML never reaches a browser or a remote service.
"""
from __future__ import annotations

import os
import re
import stat
import struct
import zipfile
import zlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_PDF_PAGES = 2000
MAX_DOCX_XML_BYTES = 32 * 1024 * 1024
OCR_RENDER_SCALE = 2.0
DEFAULT_CHUNK_CHARS = 1200
DEFAULT_CHUNK_OVERLAP = 200
MAX_CHUNK_CHARS = 4000
MIN_CHUNK_CHARS = 200

OcrPage = Callable[[bytes, int], str]

TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".json", ".jsonl", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".csv", ".tsv", ".log",
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".css",
    ".html", ".htm", ".xml", ".sql", ".ps1", ".sh", ".bat", ".cmd",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".rs", ".go", ".java",
    ".kt", ".swift",
}
SUPPORTED_SUFFIXES = TEXT_SUFFIXES | {".pdf", ".docx"}
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


@dataclass(frozen=True)
class ExtractedSection:
    text: str
    page: int | None = None
    heading: str | None = None


@dataclass(frozen=True)
class ExtractedDocument:
    title: str
    kind: str
    sections: tuple[ExtractedSection, ...]

    @property
    def text(self) -> str:
        return "\n\n".join(section.text for section in self.sections)


@dataclass(frozen=True)
class Chunk:
    text: str
    page: int | None
    heading: str | None
    char_start: int
    char_end: int
    token_estimate: int


def _is_link_entry(entry: os.DirEntry) -> bool:
    try:
        if entry.is_symlink():
            return True
        attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attrs & _REPARSE_POINT)


def is_supported(path: str | Path) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_SUFFIXES


def collect_paths(raw_paths: Iterable[str], *, max_files: int = 2000) -> list[Path]:
    """Expand files/directories, skipping links and refusing unbounded input."""
    if max_files < 1:
        raise ValueError("max_files must be positive")
    files: list[Path] = []
    seen: set[str] = set()

    def add(candidate: Path) -> None:
        resolved = candidate.resolve()
        key = os.path.normcase(str(resolved))
        if key in seen:
            return
        if not is_supported(resolved):
            return
        seen.add(key)
        files.append(resolved)
        if len(files) > max_files:
            raise ValueError(f"too many files (limit {max_files})")

    for raw in raw_paths:
        if not isinstance(raw, str) or not raw.strip():
            continue
        path = Path(raw.strip()).expanduser()
        if not path.is_absolute():
            raise ValueError(f"knowledge source path must be absolute: {raw}")
        if not path.exists():
            # 用户点名给出的路径没找到必须报错：静默跳过会让界面显示"已添加"，
            # 而那份资料其实从未进入知识库（AGENTS §2.24 不许静默丢数据）。
            # 目录遍历中消失的文件与符号链接仍按跳过处理，那是扫描不是选择。
            raise ValueError(f"knowledge source does not exist: {raw}")
        if path.is_symlink():
            continue
        try:
            root_attrs = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
        except OSError:
            continue
        if root_attrs & _REPARSE_POINT:
            continue
        if path.is_file():
            add(path)
            continue
        if not path.is_dir():
            continue
        stack = [path]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        if _is_link_entry(entry):
                            continue
                        if entry.is_dir():
                            stack.append(Path(entry.path))
                        elif entry.is_file():
                            add(Path(entry.path))
            except OSError:
                continue
        if len(files) > max_files:
            raise ValueError(f"too many files (limit {max_files})")
    return sorted(files)


def _decode_text(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    for encoding in ("utf-8-sig", "gb18030", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("text encoding is not UTF-8/UTF-16/GB18030")


class _TextHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.body_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = True
        elif tag in {"script", "style", "form", "nav", "template"}:
            self._skip_depth += 1
        elif tag in {"p", "div", "li", "br", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}:
            self.body_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        elif tag in {"script", "style", "form", "nav", "template"} and self._skip_depth:
            self._skip_depth -= 1
        elif tag in {"p", "div", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}:
            self.body_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        elif not self._skip_depth and data.strip():
            self.body_parts.append(data)


def _extract_html(path: Path, text: str) -> ExtractedDocument:
    parser = _TextHTMLParser()
    parser.feed(text)
    parser.close()
    body = re.sub(r"[ \t]+", " ", "".join(parser.body_parts)).strip()
    if not body:
        raise ValueError("HTML contains no extractable text")
    title = " ".join(parser.title_parts).strip() or path.stem
    return ExtractedDocument(title=title, kind="html", sections=(ExtractedSection(text=body),))


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def encode_png_bgr(buffer: bytes, width: int, height: int, stride: int) -> bytes:
    """Encode a pdfium BGR bitmap as a PNG without pulling in Pillow.

    libllama's image loader accepts PNG; writing the four chunks we need keeps
    the packaged runtime free of an imaging dependency just for OCR.
    """
    if width <= 0 or height <= 0 or stride < width * 3:
        raise ValueError("invalid bitmap dimensions")
    row_bytes = width * 3
    raw = bytearray()
    for y in range(height):
        start = y * stride
        line = buffer[start:start + row_bytes]
        if len(line) != row_bytes:
            raise ValueError("truncated bitmap buffer")
        rgb = bytearray(row_bytes)
        rgb[0::3] = line[2::3]
        rgb[1::3] = line[1::3]
        rgb[2::3] = line[0::3]
        raw.append(0)  # filter type: None
        raw.extend(rgb)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + _png_chunk(b"IEND", b"")
    )


def render_pdf_page(path: str | Path, page_index: int, *, scale: float = OCR_RENDER_SCALE) -> bytes:
    """Render one 0-based PDF page to PNG for OCR."""
    if not isinstance(page_index, int) or page_index < 0:
        raise ValueError("page_index must be a non-negative integer")
    try:
        import pypdfium2 as pdfium
    except ImportError as error:  # pragma: no cover - dependency is locked
        raise ValueError("scanned PDFs require the pypdfium2 package") from error
    try:
        document = pdfium.PdfDocument(str(path))
    except Exception as error:  # noqa: BLE001 - pdfium reports a single error type
        raise ValueError(f"PDF could not be opened for rendering: {error}") from error
    try:
        if page_index >= len(document):
            raise ValueError("PDF page index is out of range")
        page = document[page_index]
        bitmap = page.render(scale=max(0.5, min(float(scale), 4.0)))
        try:
            return encode_png_bgr(
                bytes(bitmap.buffer),
                int(bitmap.width),
                int(bitmap.height),
                int(bitmap.stride),
            )
        finally:
            bitmap.close()
            page.close()
    finally:
        document.close()


def _extract_pdf(path: Path, ocr: OcrPage | None = None) -> ExtractedDocument:
    try:
        from pypdf import PdfReader
    except ImportError as error:  # pragma: no cover - dependency is locked
        raise ValueError("PDF support requires the pypdf package") from error
    try:
        reader = PdfReader(str(path))
        if len(reader.pages) > MAX_PDF_PAGES:
            raise ValueError(f"PDF has too many pages (limit {MAX_PDF_PAGES})")
        sections: list[ExtractedSection] = []
        ocr_pages = 0
        for index, page in enumerate(reader.pages[:MAX_PDF_PAGES], start=1):
            try:
                text = page.extract_text() or ""
            except Exception:  # noqa: BLE001 - one bad page must not kill the document
                text = ""
            text = text.strip()
            if not text and ocr is not None:
                png = render_pdf_page(path, index - 1)
                try:
                    text = str(ocr(png, index) or "").strip()
                except Exception as error:  # noqa: BLE001 - OCR service/driver failures
                    raise ValueError(f"OCR failed on page {index}: {error}") from error
                if text:
                    ocr_pages += 1
            if text:
                sections.append(ExtractedSection(text=text, page=index))
        if not sections:
            if ocr is None:
                raise ValueError(
                    "PDF has no extractable text; scanned PDFs need an OCR model"
                )
            raise ValueError("PDF has no extractable text after OCR")
    except ValueError:
        raise
    except Exception as error:  # noqa: BLE001 - pypdf has many malformed-input exceptions
        raise ValueError(f"PDF could not be read: {error}") from error
    metadata_title = ""
    try:
        metadata_title = str((reader.metadata or {}).get("/Title") or "").strip()
    except Exception:  # noqa: BLE001
        pass
    return ExtractedDocument(
        title=metadata_title or path.stem,
        kind="pdf" if not ocr_pages else "pdf-ocr",
        sections=tuple(sections),
    )


def _docx_paragraphs(xml: bytes) -> list[tuple[str | None, str]]:
    root = ElementTree.fromstring(xml)
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paragraphs: list[tuple[str | None, str]] = []
    heading: str | None = None
    for paragraph in root.iter(f"{ns}p"):
        texts = [node.text or "" for node in paragraph.iter(f"{ns}t")]
        text = "".join(texts).strip()
        if not text:
            continue
        style = ""
        ppr = paragraph.find(f"{ns}pPr")
        if ppr is not None:
            style_node = ppr.find(f"{ns}pStyle")
            if style_node is not None:
                style = str(style_node.attrib.get(f"{ns}val") or "")
        if style.lower().startswith("heading") or style.lower() == "title":
            heading = text
        paragraphs.append((heading, text))
    return paragraphs


def _extract_docx(path: Path) -> ExtractedDocument:
    try:
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > MAX_DOCX_XML_BYTES:
                raise ValueError("DOCX document XML is too large")
            xml = archive.read(info)
            if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
                raise ValueError("DOCX XML entities are not supported")
    except KeyError as error:
        raise ValueError("DOCX is missing word/document.xml") from error
    except (OSError, zipfile.BadZipFile) as error:
        raise ValueError(f"DOCX could not be read: {error}") from error
    paragraphs = _docx_paragraphs(xml)
    if not paragraphs:
        raise ValueError("DOCX contains no extractable text")
    sections = tuple(
        ExtractedSection(text=text, heading=heading)
        for heading, text in paragraphs
    )
    return ExtractedDocument(title=path.stem, kind="docx", sections=sections)


def extract_file(
    path: str | Path,
    *,
    max_bytes: int = MAX_FILE_BYTES,
    ocr: OcrPage | None = None,
) -> ExtractedDocument:
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError("knowledge source must be a regular file")
    try:
        size = candidate.stat().st_size
    except OSError as error:
        raise ValueError(f"file could not be inspected: {error}") from error
    if size > max_bytes:
        raise ValueError(f"file is too large (limit {max_bytes} bytes)")
    suffix = candidate.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError(f"unsupported knowledge file type: {suffix or '(none)'}")

    if suffix == ".pdf":
        return _extract_pdf(candidate, ocr)
    if suffix == ".docx":
        return _extract_docx(candidate)

    try:
        data = candidate.read_bytes()
    except OSError as error:
        raise ValueError(f"file could not be read: {error}") from error
    if b"\x00" in data:
        raise ValueError("binary files are not supported")
    text = _decode_text(data)
    if suffix in {".html", ".htm"}:
        return _extract_html(candidate, text)
    text = text.strip()
    if not text:
        raise ValueError("file contains no extractable text")
    title = candidate.stem
    heading_match = re.search(r"^\s*#\s+(.+)$", text, re.MULTILINE)
    if heading_match:
        title = heading_match.group(1).strip() or title
    return ExtractedDocument(
        title=title,
        kind="text",
        sections=(ExtractedSection(text=text),),
    )


_LEGAL_MARKER_RE = re.compile(r"第[一二三四五六七八九十百千零〇\d]+(?:章|节|条)")


def _article_label(text: str, start: int, end: int) -> str:
    """The chapter or article occupying most of this chunk, for citation headings.

    Measured on the legal KB: splitting chunks *on* article boundaries hurt the
    no-rerank path (hybrid hit@1 52% -> 35%) because small article chunks dilute
    both arms, while keeping windowed chunks and only *labelling* them gives the
    same citation clarity for free. "Most of the chunk" matters: a window that
    begins mid-article and only brushes into the next one must still be
    attributed to the article it is mostly made of.
    """
    markers = list(_LEGAL_MARKER_RE.finditer(text))
    if not markers:
        return ""
    bounds = [m.start() for m in markers] + [len(text)]
    best_label = ""
    best_overlap = 0
    if markers[0].start() > start:
        # The chunk opens inside an article that began in an earlier chunk.
        preceding = [m for m in markers if m.start() < start]
        if preceding:
            best_label = preceding[-1].group(0)
            best_overlap = min(end, markers[0].start()) - start
    for index, marker in enumerate(markers):
        overlap = min(end, bounds[index + 1]) - max(start, bounds[index])
        if overlap > best_overlap:
            best_label = marker.group(0)
            best_overlap = overlap
    return best_label if best_overlap > 0 else ""


def chunk_sections(
    sections: Iterable[ExtractedSection],
    *,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[Chunk]:
    """Split sections into bounded, overlapping chunks with source metadata."""
    size = max(MIN_CHUNK_CHARS, min(int(chunk_chars or DEFAULT_CHUNK_CHARS), MAX_CHUNK_CHARS))
    step = max(0, min(int(overlap or 0), size - 1))
    chunks: list[Chunk] = []
    for section in sections:
        text = re.sub(r"\n{3,}", "\n\n", section.text or "").strip()
        if not text:
            continue
        start = 0
        while start < len(text):
            end = min(start + size, len(text))
            if end < len(text):
                window = text[start:end]
                split_at = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(" "))
                if split_at >= size // 2:
                    end = start + split_at
            piece = text[start:end].strip()
            if piece:
                chunks.append(
                    Chunk(
                        text=piece,
                        page=section.page,
                        heading=_article_label(text, start, end) or section.heading,
                        char_start=start,
                        char_end=end,
                        token_estimate=max(1, (len(piece) + 2) // 3),
                    )
                )
            if end >= len(text):
                break
            next_start = max(start + 1, end - step)
            start = next_start
    return chunks
