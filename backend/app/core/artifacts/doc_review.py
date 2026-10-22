"""AI 文档审阅（Agent 广场 `doc-review` 能力）的 docx 解析与回写核心。

设计原则（踩坑结论）：**docx 不走 HTML 中转**。docx → HTML → docx 的 round-trip 会把
样式、表格、页眉页脚全部丢掉；正确做法是把文档解析成「段落块（block）」交给 AI，
AI 只产出「块 id + 新文本」，导出时在**原文档对象**上做段落级文本替换 —— 格式天然保留。

代价（已知取舍）：同一段落内的混合格式（局部加粗/变色）回写时会归并为该段首 run 的格式。
段落间格式（标题级别、列表、表格结构、图片）完全不受影响。
"""

from __future__ import annotations

import time
import uuid
from io import BytesIO
from typing import Any, Literal

from pydantic import BaseModel, Field

#: 单个文档的段落数上限：超过直接拒绝，防止把内存与 LLM 上下文撑爆。
MAX_BLOCKS = 1200
#: 上传文件大小上限（20MB，Word 文档正常不会超过）。
MAX_DOCX_BYTES = 20 * 1024 * 1024

DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class DocBlock(BaseModel):
    """一个可见段落。`id` 是会话内稳定的短 id（"p0"、"p1"…），前后端与 AI 都用它寻址。"""

    id: str
    text: str
    kind: Literal["heading", "paragraph", "list"] = "paragraph"
    level: int = 0
    #: 是否位于表格单元格内（审阅与回写都要覆盖表格内容）。
    in_table: bool = False
    #: 修订追踪模式下，AI 修改以 Word 批注（comment）形式附在该段落上，不改原文。
    #: 空串 = 无批注。事实来源在前端，随导出/渲染请求带回。
    comment: str = ""


class DocIssue(BaseModel):
    """一键审阅产出的一条问题。`fixes` 为空表示只有建议、没有文字级修改。"""

    id: str
    type: str  # 错别字 / 语病 / 标点 / 表述 / 风险 / 结构 …
    block_ids: list[str] = Field(default_factory=list)
    title: str
    detail: str = ""
    fixes: list[dict[str, str]] = Field(default_factory=list)  # [{block_id, new_text}]


class DocAction(BaseModel):
    """对话修改返回的一个操作：把某个块替换成新文本。"""

    block_id: str
    new_text: str
    reason: str = ""


class ChatOutcome(BaseModel):
    reply: str
    actions: list[DocAction] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 内存文档仓：doc_id → 原始 Document 对象 + 段落元素索引。
#
# 只存 docx 二进制与段落对象映射，**不存任何 AI 产物** —— blocks 的唯一事实来源在
# 前端（与 slides 的 deck 模式一致），导出时随请求带回来。单进程部署下够用；
# 超过容量按最久未使用淘汰。
# ---------------------------------------------------------------------------

_STORE_MAX = 20
_DOC_STORE: dict[str, dict[str, Any]] = {}


def store_document(file_name: str, document: Any, paragraphs: list[Any]) -> str:
    doc_id = uuid.uuid4().hex[:12]
    _DOC_STORE[doc_id] = {
        "name": file_name,
        "document": document,
        "paragraphs": paragraphs,
        "touched_at": time.time(),
    }
    while len(_DOC_STORE) > _STORE_MAX:
        oldest = min(_DOC_STORE, key=lambda key: _DOC_STORE[key]["touched_at"])
        _DOC_STORE.pop(oldest, None)
    return doc_id


def get_stored_document(doc_id: str) -> dict[str, Any] | None:
    entry = _DOC_STORE.get(doc_id)
    if entry is not None:
        entry["touched_at"] = time.time()
    return entry


# ---------------------------------------------------------------------------
# 解析：docx → blocks（document 顺序，含表格内段落）
# ---------------------------------------------------------------------------


def _iter_document_paragraphs(document: Any) -> list[tuple[Any, bool]]:
    """按文档顺序收集所有段落 `(Paragraph, 是否表格内)`。

    只走 body 的直接子元素：顶层 `w:p` 与 `w:tbl`（表格内再逐 cell 取段落）。
    合并单元格会在 `row.cells` 里重复出现，用底层 tc 元素去重。
    嵌套表格按平铺处理（MVP 不递归）。
    """
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    result: list[tuple[Any, bool]] = []
    seen_tc: set[int] = set()
    body = document.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            result.append((Paragraph(child, document), False))
        elif child.tag == qn("w:tbl"):
            table = Table(child, document)
            for row in table.rows:
                for cell in row.cells:
                    if id(cell._tc) in seen_tc:
                        continue
                    seen_tc.add(id(cell._tc))
                    for paragraph in cell.paragraphs:
                        result.append((paragraph, True))
    return result


def _block_kind(paragraph: Any) -> tuple[Literal["heading", "paragraph", "list"], int]:
    style_name = (paragraph.style.name or "").lower() if paragraph.style is not None else ""
    if style_name.startswith("heading"):
        digits = "".join(char for char in style_name if char.isdigit())
        return "heading", int(digits) if digits else 1
    # numPr = 编号/项目符号列表
    if paragraph._p.find(".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}numPr") is not None:
        return "list", 0
    if "list" in style_name or "toc" in style_name:
        return "list", 0
    return "paragraph", 0


def _paragraph_text(paragraph: Any) -> str:
    return "".join(run.text for run in paragraph.runs)


def parse_docx(data: bytes) -> tuple[str, list[DocBlock], Any, list[Any]]:
    """解析 docx 二进制。返回 `(文件名, blocks, Document 对象, 段落对象列表)`。"""
    from docx import Document

    if len(data) > MAX_DOCX_BYTES:
        raise ValueError("文件超过 20MB，请拆分后再上传")
    try:
        document = Document(BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - 损坏/非 docx 文件统一转成用户可读文案
        raise ValueError("无法解析该文件：请确认上传的是 .docx（老 .doc 格式请先用 Word 另存为 .docx）") from exc

    core_title = ""
    try:
        core_title = (document.core_properties.title or "").strip()
    except Exception:  # noqa: BLE001 - 元数据缺失不影响解析
        core_title = ""

    pairs = _iter_document_paragraphs(document)
    blocks: list[DocBlock] = []
    kept_paragraphs: list[Any] = []
    for paragraph, in_table in pairs:
        text = _paragraph_text(paragraph).strip()
        # 空段落（含纯空格）不进 blocks：AI 寻址更干净，回写时也无需处理
        if not text:
            continue
        if len(blocks) >= MAX_BLOCKS:
            raise ValueError(f"文档段落数超过 {MAX_BLOCKS}，请拆分后再上传")
        kind, level = _block_kind(paragraph)
        blocks.append(
            DocBlock(id=f"p{len(blocks)}", text=text, kind=kind, level=level, in_table=in_table)
        )
        kept_paragraphs.append(paragraph)

    if not blocks:
        raise ValueError("文档里没有可审阅的文字内容")

    name = core_title or "document"
    return name, blocks, document, kept_paragraphs


# ---------------------------------------------------------------------------
# 回写：blocks（前端事实来源）→ 在原 Document 上做段落级文本替换
# ---------------------------------------------------------------------------

#: AI 批注的作者名：既是 Word 里展示的作者，也是导出时识别「哪些批注归我们管」的依据。
COMMENT_AUTHOR = "AI 审阅"
#: 单条批注长度上限（防 AI 跑飞的长文本把 comments.xml 撑爆）。
MAX_COMMENT_CHARS = 2000


def set_paragraph_text(paragraph: Any, text: str) -> None:
    """替换段落文本，保留首 run 的字符格式（字体/字号/加粗）。

    逐 run 清空再改第一个 run，而不是 `paragraph.text = text`（后者会重置整段格式）。
    """
    runs = list(paragraph.runs)
    if not runs:
        paragraph.add_run(text)
        return
    for run in runs[1:]:
        run._element.getparent().remove(run._element)
    runs[0].text = text
    # 首 run 若是纯空格排版残留，清掉多余前导空白
    if runs[0].text != text:
        runs[0].text = text


def _remove_ai_comments(document: Any) -> None:
    """删掉文档里所有 AI 批注（按作者名识别），用户自己的批注原样保留。

    批注由三部分组成：comments.xml 里的 `w:comment` 条目 + 正文里的
    `w:commentRangeStart/End` 定界符 + `w:commentReference` 引用（住在某个 run 里）。
    每次导出先清后加，保证同一批 blocks 反复导出不会累积重复批注。
    """
    from docx.oxml.ns import qn

    comments_part = getattr(document.part, "_comments_part", None)
    if comments_part is None:
        return
    root = comments_part._element  # w:comments
    ai_ids: set[str] = set()
    for comment in list(root.findall(qn("w:comment"))):
        author = (comment.get(qn("w:author")) or "").strip()
        if author == COMMENT_AUTHOR:
            comment_id = comment.get(qn("w:id"))
            if comment_id is not None:
                ai_ids.add(comment_id)
            root.remove(comment)
    if not ai_ids:
        return

    body = document.element.body
    for tag in ("w:commentRangeStart", "w:commentRangeEnd"):
        for element in list(body.iter(qn(tag))):
            if element.get(qn("w:id")) in ai_ids:
                parent = element.getparent()
                if parent is not None:
                    parent.remove(element)
    for reference in list(body.iter(qn("w:commentReference"))):
        if reference.get(qn("w:id")) in ai_ids:
            run = reference.getparent()
            if run is not None and run.tag == qn("w:r"):
                parent = run.getparent()
                if parent is not None:
                    parent.remove(run)


def _apply_comments(document: Any, paragraphs: list[Any], blocks: list[DocBlock]) -> int:
    """把 blocks 里的批注字段写成 Word 原生批注。返回成功落批注的段落数。

    先清掉旧的 AI 批注再重加（幂等）；锚定整段（该段全部 runs）。段内没有 run
    （理论上不会出现：非空段必然有文字 run）就跳过，不影响文本回写。
    """
    _remove_ai_comments(document)
    by_id = {block.id: block for block in blocks}
    added = 0
    for index, paragraph in enumerate(paragraphs):
        block = by_id.get(f"p{index}")
        comment_text = (block.comment if block else "").strip()
        if not comment_text:
            continue
        runs = list(paragraph.runs)
        if not runs:
            continue
        try:
            document.add_comment(
                runs,
                text=comment_text[:MAX_COMMENT_CHARS],
                author=COMMENT_AUTHOR,
                initials="AI",
            )
            added += 1
        except Exception:  # noqa: BLE001 - 单条批注失败不拖垮导出
            continue
    return added


def apply_blocks_to_document(entry: dict[str, Any], blocks: list[DocBlock]) -> int:
    """把（可能被 AI/用户改过的）blocks 回写到存储的 Document 上。返回实际改写的段落数。

    通过块 id 定位段落（parse 时 `p{i}` 与段落列表一一对应），文本没变的块不动 ——
    所以未修改段落的格式/混合 run 完全不受影响。修订追踪模式下 blocks 还会带
    `comment`（AI 修改以批注形式落在原文旁，不改文字），同样在这里回写。
    """
    paragraphs: list[Any] = entry["paragraphs"]
    by_id = {block.id: block for block in blocks}
    changed = 0
    for index, paragraph in enumerate(paragraphs):
        block = by_id.get(f"p{index}")
        if block is None:
            continue
        current = _paragraph_text(paragraph)
        if current != block.text:
            set_paragraph_text(paragraph, block.text)
            changed += 1
    _apply_comments(document=entry["document"], paragraphs=paragraphs, blocks=blocks)
    return changed


def document_to_bytes(entry: dict[str, Any]) -> bytes:
    buffer = BytesIO()
    entry["document"].save(buffer)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# 审阅 / 对话的提示词与收敛
# ---------------------------------------------------------------------------

REVIEW_SYSTEM_PROMPT = """你是资深中文文档审阅专家。请逐段检查给定文档内容，找出确实存在的问题：
- 错别字 / 用词错误
- 语病、成分残缺、搭配不当
- 标点误用
- 表述不清、指代不明、逻辑跳跃
- 明显的事实/数据风险（如前后矛盾、单位错误）

硬性要求：
1. 只报告有把握的问题，**禁止为了凑数编造问题**；没有问题就返回空 issues。
2. 每个问题给出涉及的段落 id（block_ids）与简短说明。
3. 若问题可以落成文字修改，给出 fixes：把受影响段落改成的新文本（整段替换口径，不是 diff）。
4. 不要改写风格、不要重写内容、不要提出「建议增加章节」这类无法落段的意见（那类归入 detail 说明即可，不带 fixes）。
5. 输出 JSON：{"issues": [{"block_ids": ["p3"], "type": "错别字", "title": "≤20字的问题概括", "detail": "说明", "fixes": [{"block_id": "p3", "new_text": "..."}]}]}"""

CHAT_SYSTEM_PROMPT = """你是文档协作助手，用户会对右侧文档提出修改需求（修正错别字、归纳总结、润色、改写等）。
你只能通过「段落替换」操作文档，不能增删段落、不能改格式。

返回 JSON：
{"reply": "给用户的简短说明（做了什么/为什么）",
 "actions": [{"block_id": "p5", "new_text": "替换后的整段文本", "reason": "≤16字的改动原因"}]}

要求：
1. 只对需要修改的段落给出 action；没有需要改动时就返回空 actions，用 reply 说明。
2. new_text 是**整段替换后的完整文本**，不是 diff，不要省略号。
3. 归纳总结若要落到文档里，替换指定段落（用户指明的段落，或适合承载总结的段落），并在 reply 里说明改了哪里。
4. 指令模糊时宁可少改：在 reply 里确认理解，不猜着改。
5. 保持原文的语言与文体，除非用户明确要求改风格。"""


def review_batch_size() -> int:
    return 40


def sanitize_issues(raw: Any, blocks: list[DocBlock]) -> list[DocIssue]:
    """收敛 LLM 的审阅返回：字段容错、block_id 必须真实存在、fixes 的 new_text 不能为空。"""
    known_ids = {block.id for block in blocks}
    issues: list[DocIssue] = []
    if not isinstance(raw, dict):
        return issues
    rows = raw.get("issues")
    if not isinstance(rows, list):
        return issues
    for position, row in enumerate(rows[:60]):
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        block_ids = [
            str(block_id) for block_id in (row.get("block_ids") or []) if str(block_id) in known_ids
        ]
        fixes: list[dict[str, str]] = []
        for fix in row.get("fixes") or []:
            if not isinstance(fix, dict):
                continue
            fix_block = str(fix.get("block_id") or "")
            new_text = str(fix.get("new_text") or "").strip()
            if fix_block in known_ids and new_text:
                fixes.append({"block_id": fix_block, "new_text": new_text})
        issues.append(
            DocIssue(
                id=f"i{position}",
                type=str(row.get("type") or "问题").strip()[:12],
                block_ids=block_ids,
                title=title[:120],
                detail=str(row.get("detail") or "").strip()[:500],
                fixes=fixes,
            )
        )
    return issues


def sanitize_actions(raw: Any, blocks: list[DocBlock]) -> ChatOutcome:
    """收敛对话返回：action 的 block_id 必须存在、new_text 非空且不得显著超出原段落长度（防跑飞）。"""
    known = {block.id: block for block in blocks}
    if not isinstance(raw, dict):
        return ChatOutcome(reply="模型返回格式异常，请重试。")
    reply = str(raw.get("reply") or "").strip() or "已完成。"
    actions: list[DocAction] = []
    for row in (raw.get("actions") or [])[:80]:
        if not isinstance(row, dict):
            continue
        block_id = str(row.get("block_id") or "")
        new_text = str(row.get("new_text") or "").strip()
        block = known.get(block_id)
        if block is None or not new_text:
            continue
        if len(new_text) > max(len(block.text) * 4 + 400, 4000):
            # 单段膨胀超过 4 倍基本是模型跑飞了，丢弃该 action
            continue
        actions.append(
            DocAction(block_id=block_id, new_text=new_text, reason=str(row.get("reason") or "").strip()[:40])
        )
    return ChatOutcome(reply=reply[:2000], actions=actions)
