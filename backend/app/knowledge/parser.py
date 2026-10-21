from __future__ import annotations

import re
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile


SUPPORTED_EXTENSIONS = {".txt", ".md", ".markdown", ".html", ".htm", ".pdf", ".docx", ".doc"}


class KnowledgeParseError(ValueError):
    pass


def extract_text(filename: str, content: bytes) -> tuple[str, str]:
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise KnowledgeParseError(f"暂不支持 {suffix or 'unknown'} 文件格式。")
    if suffix == ".doc":
        raise KnowledgeParseError("暂不支持旧版 .doc 二进制格式，请转换为 .docx 后上传。")
    if suffix in {".txt", ".md", ".markdown"}:
        return _decode_text(content), suffix.lstrip(".")
    if suffix in {".html", ".htm"}:
        return _extract_html(content), "html"
    if suffix == ".pdf":
        return _extract_pdf(content), "pdf"
    if suffix == ".docx":
        return _extract_docx(content), "docx"
    raise KnowledgeParseError(f"暂不支持 {suffix} 文件格式。")


def _decode_text(content: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "gb18030", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="ignore")


def _extract_html(content: bytes) -> str:
    text = _decode_text(content)
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(text, "html.parser")
        for item in soup(["script", "style", "noscript"]):
            item.decompose()
        return soup.get_text("\n")
    except Exception:
        parser = _HTMLTextExtractor()
        parser.feed(text)
        return parser.text


def _extract_pdf(content: bytes) -> str:
    try:
        from pypdf import PdfReader
    except Exception as exc:  # pragma: no cover - dependency availability differs by env.
        raise KnowledgeParseError("缺少 pypdf，无法解析 PDF。") from exc
    reader = PdfReader(BytesIO(content))
    pages: list[str] = []
    for index, page in enumerate(reader.pages):
        page_text = page.extract_text() or ""
        if page_text.strip():
            pages.append(f"## 第 {index + 1} 页\n\n{page_text}")
    if not pages:
        return ""
    return "# PDF 文档\n\n" + "\n\n".join(pages)


def _extract_docx(content: bytes) -> str:
    """抽取 .docx 正文。

    三条路径依次尝试：
    1. python-docx（正常文档，保留标题层级与表格）；
    2. 裸 zip 解析 `word/document.xml`（python-docx 装不上或文档结构异常时兜底）；
    3. 纯文本兜底 —— 后缀写成 `.docx`、内容其实是文本（把 .txt / 网页复制内容直接改名）
       太常见了，与其报「无法解析」，不如按文本读进来。
    """
    if not content:
        raise KnowledgeParseError("docx 文档内容为空。")
    try:
        return _extract_docx_with_python_docx(content)
    except Exception:  # noqa: BLE001 - 逐级降级，最后统一给可读报错
        pass
    try:
        return _extract_docx_with_zip(content)
    except Exception as exc:  # noqa: BLE001
        plain = _decode_docx_as_plain_text(content)
        if plain is not None:
            return plain
        raise KnowledgeParseError(_docx_error_message(content)) from exc


def _extract_docx_with_python_docx(content: bytes) -> str:
    from docx import Document

    document = Document(BytesIO(content))
    rows: list[str] = []
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        heading_level = _docx_heading_level(paragraph.style.name if paragraph.style else "")
        rows.append(f"{'#' * heading_level} {text}" if heading_level else text)
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                rows.append(" | ".join(cells))
    return "\n".join(rows)


def _decode_docx_as_plain_text(content: bytes) -> str | None:
    """把「其实是纯文本」的 .docx 当文本读；不像文本则返回 None。

    用 NUL 字节先排掉二进制（ZIP / PDF / 旧版 .doc 里一定会有），
    再看可打印字符占比，避免把乱码当成正文喂进知识库。
    """
    sample = content[:4096]
    if b"\x00" in sample:
        return None
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = content.decode(encoding)
        except UnicodeDecodeError:
            continue
        if _looks_like_text(text):
            return text
    return None


def _looks_like_text(text: str) -> bool:
    sample = text[:4096]
    if not sample.strip():
        return False
    printable = sum(1 for char in sample if char.isprintable() or char in "\r\n\t")
    return printable / len(sample) >= 0.95


def _docx_error_message(content: bytes) -> str:
    """按真实文件头给出可执行的报错，而不是笼统的「无法解析」。"""
    head = content[:8]
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "这个文件其实是旧版 .doc 格式，请用 Word 另存为 .docx 后重新上传。"
    if head.startswith(b"%PDF"):
        return "这个文件其实是 PDF，请把后缀改回 .pdf 后重新上传。"
    if head.startswith(b"PK"):
        return "docx 文件已损坏（缺少 word/document.xml），请用 Word 另存为 .docx 后重新上传。"
    return (
        "无法解析该文件：它不是有效的 .docx（OOXML）包，也不像纯文本。"
        "请用 Word 另存为 .docx，或直接上传 .txt / .md。"
    )


def _docx_heading_level(style_name: str) -> int | None:
    match = re.match(r"^(?:Heading|标题)\s*([1-6])$", style_name.strip(), re.IGNORECASE)
    return int(match.group(1)) if match else None


def _extract_docx_with_zip(content: bytes) -> str:
    """不装 python-docx 时的兜底：直接读 `word/document.xml` 的文本节点。

    失败时把原始异常抛给 `_extract_docx`，由它决定是否降级成纯文本、以及给什么报错。
    """
    with ZipFile(BytesIO(content)) as archive:
        xml = archive.read("word/document.xml").decode("utf-8", errors="ignore")
    parser = _DocxTextExtractor()
    parser.feed(xml)
    return parser.text


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    @property
    def text(self) -> str:
        return "\n".join(part.strip() for part in self._parts if part.strip())

    def handle_data(self, data: str) -> None:
        if data.strip():
            self._parts.append(data)


class _DocxTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    @property
    def text(self) -> str:
        return "\n".join(part.strip() for part in self._parts if part.strip())

    def handle_data(self, data: str) -> None:
        if data.strip():
            self._parts.append(data)
