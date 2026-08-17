"""Markdown 子集解析器 + 飞书 post 富文本渲染。

设计目标（见 channel-render-plan-feishu-dingtalk.md §3.2）：
- 零新增依赖，手写解析器覆盖受控子集；
- 输出通用块模型供飞书渲染器消费（钉钉原生 markdown 直接透传，不走块模型）；
- `has_markdown(text)` 做语法检测，纯文本返回 False 以走原 text 路径，避免回归。

覆盖子集：标题 / 粗体 / 斜体 / 行内代码 / 围栏代码块 / 链接 / 有序无序列表 / 引用 / 分隔线。
不支持的（表格、HTML、不闭合围栏等）按纯文本处理，绝不抛异常。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Span:
    """行内文本片段。styles 为空集合表示普通文本。"""

    text: str
    styles: frozenset[str] = field(default_factory=frozenset)
    href: str = ""


@dataclass
class Heading:
    level: int
    spans: list[Span]


@dataclass
class Paragraph:
    spans: list[Span]


@dataclass
class CodeBlock:
    language: str
    text: str


@dataclass
class ListItem:
    ordered: bool
    index: int
    spans: list[Span]


@dataclass
class Quote:
    spans: list[Span]


@dataclass
class ThematicBreak:
    pass


@dataclass
class TableBlock:
    """表格降级：按纯文本行保留，飞书 post 不支持表格。"""

    lines: list[str]


Block = Heading | Paragraph | CodeBlock | ListItem | Quote | ThematicBreak | TableBlock


# 行级正则
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^(`{3,}|~{3,})\s*([\w+-]*)\s*$")
_UNORDERED_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
_ORDERED_RE = re.compile(r"^\s*(\d+)\.\s+(.*)$")
_QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")
_HR_RE = re.compile(r"^\s*([-*_])\1{2,}\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
_INDENT_CODE_RE = re.compile(r"^(    |\t)(.*)$")

# 顶格代码起始行检测：def/class/import/from/if __name__/function/func 等明显的代码关键字
_CODE_START_RE = re.compile(
    r"^(def |class |import |from \S+ import |if __name__|async def |@)"
)

# 行内正则
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*([^*\n]+?)\*\*|__([^_\n]+?)__")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)|(?<!_)_([^_\n]+?)_(?!_)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
_CODE_PLACEHOLDER_RE = re.compile(r"\x00CODE(\d+)\x00")

# has_markdown 检测：命中任一即判定为 markdown
_MD_DETECT_PATTERNS = [
    re.compile(r"(?m)^\s{0,3}#{1,6}\s+\S"),  # 标题
    re.compile(r"(?m)^\s{0,3}[-*+]\s+\S"),  # 无序列表
    re.compile(r"(?m)^\s{0,3}\d+\.\s+\S"),  # 有序列表
    re.compile(r"(?m)^\s{0,3}>\s?\S"),  # 引用
    re.compile(r"(?m)^\s{0,3}([-*_])\1{2,}\s*$"),  # 分隔线
    re.compile(r"(?m)^\s*```"),  # 围栏代码块
    re.compile(r"(?m)^(def |class |import |from \S+ import |if __name__|async def |@)"),  # 顶格代码
    re.compile(r"\*\*[^*\n]+\*\*|__[^_\n]+__"),  # 粗体
    re.compile(r"`[^`\n]+`"),  # 行内代码
    re.compile(r"\[[^\]]*\]\([^)\s]+\)"),  # 链接
]


def has_markdown(text: str) -> bool:
    """检测文本是否含 markdown 语法标记。

    纯文本（含普通破折号、单星号装饰但不构成语法）应返回 False，避免外观变化。
    斜体 `*x*` / `_x_` 不参与检测，因其误判率高（如 "a * b"、文件名 a_b_c）。
    """
    if not text:
        return False
    return any(pattern.search(text) for pattern in _MD_DETECT_PATTERNS)


def _is_markdown_block_line(line: str) -> bool:
    """判断一行是否是 markdown 块级语法行（标题/围栏/列表/引用/分隔线）。

    用于顶格代码块收集时判断后续行是否属于代码还是 markdown 语法。
    """
    return bool(
        _HEADING_RE.match(line)
        or _FENCE_RE.match(line)
        or _HR_RE.match(line)
        or _UNORDERED_RE.match(line)
        or _ORDERED_RE.match(line)
        or _QUOTE_RE.match(line)
    )


def _is_code_continuation(line: str) -> bool:
    """判断一行是否可以作为顶格代码块的续行。

    保守策略：接受缩进行、代码起始行、以 # 开头的注释行，
    以及顶格的赋值/函数调用行（含 = 或以 print/return/await/yield 开头）。
    其他顶格行（自然语言段落等）视为代码块结束。
    """
    if not line.strip():
        return False
    if _INDENT_CODE_RE.match(line):
        return True
    if _CODE_START_RE.match(line):
        return True
    # # 开头的行在代码上下文中是注释，不是标题
    if re.match(r"^#\s", line):
        return True
    # 顶格赋值行：var = ... / var: type = ...
    stripped = line.strip()
    if re.match(r"^\w[\w.]*\s*[:=]", stripped):
        return True
    # 顶格函数调用行：print(...) / foo(...) / await ...
    if re.match(r"^(print|return|await|yield|raise|break|continue)\b", stripped):
        return True
    return bool(re.match(r"^\w[\w.]*\s*\(", stripped))


def parse_markdown(text: str) -> list[Block]:
    """把 markdown 文本解析为块模型列表。不抛异常，无法解析的行降级为 Paragraph。"""
    if not text:
        return []
    lines = text.split("\n")
    blocks: list[Block] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        # 围栏代码块
        fence_match = _FENCE_RE.match(line)
        if fence_match:
            fence_marker = fence_match.group(1)
            fence_char = re.escape(fence_marker[0])
            language = fence_match.group(2) or ""
            code_lines: list[str] = []
            i += 1
            while i < n:
                cur = lines[i]
                if re.match(rf"^\s*{fence_char}{{3,}}\s*$", cur):
                    i += 1
                    break
                code_lines.append(cur)
                i += 1
            # 不闭合围栏：把已收集的行作为代码块返回，飞书仍可渲染
            blocks.append(CodeBlock(language=language, text="\n".join(code_lines)))
            continue

        # 分隔线
        if _HR_RE.match(line):
            blocks.append(ThematicBreak())
            i += 1
            continue

        # 表格（含分隔行 |---|）
        if "|" in line and i + 1 < n and _TABLE_SEP_RE.match(lines[i + 1]):
            table_lines: list[str] = []
            table_lines.append(line.strip())
            i += 1
            table_lines.append(lines[i].strip())  # 分隔行
            i += 1
            while i < n and "|" in lines[i] and lines[i].strip():
                table_lines.append(lines[i].strip())
                i += 1
            blocks.append(TableBlock(lines=table_lines))
            continue

        # 标题
        heading_match = _HEADING_RE.match(line)
        if heading_match:
            level = len(heading_match.group(1))
            content = heading_match.group(2)
            spans = _parse_inline(content)
            blocks.append(Heading(level=level, spans=spans))
            i += 1
            continue

        # 引用（连续行合并为单个 Quote）
        quote_match = _QUOTE_RE.match(line)
        if quote_match:
            quote_text_parts: list[str] = [quote_match.group(1)]
            i += 1
            while i < n:
                qm = _QUOTE_RE.match(lines[i])
                if not qm:
                    break
                quote_text_parts.append(qm.group(1))
                i += 1
            spans = _parse_inline(" ".join(part for part in quote_text_parts if part))
            blocks.append(Quote(spans=spans))
            continue

        # 无序列表（连续项各自成块，便于飞书分行）
        unordered_match = _UNORDERED_RE.match(line)
        if unordered_match:
            spans = _parse_inline(unordered_match.group(1))
            blocks.append(ListItem(ordered=False, index=0, spans=spans))
            i += 1
            continue

        # 有序列表
        ordered_match = _ORDERED_RE.match(line)
        if ordered_match:
            idx = int(ordered_match.group(1))
            spans = _parse_inline(ordered_match.group(2))
            blocks.append(ListItem(ordered=True, index=idx, spans=spans))
            i += 1
            continue

        # 空行
        if not line.strip():
            i += 1
            continue

        # 缩进代码块（4 空格或 tab）：连续缩进行（含中间空行）合并为代码块
        if _INDENT_CODE_RE.match(line):
            code_lines: list[str] = []
            while i < n:
                cur = lines[i]
                m = _INDENT_CODE_RE.match(cur)
                if m:
                    code_lines.append(m.group(2))
                    i += 1
                elif not cur.strip():
                    # 收集连续空行，看后面是否还有缩进行
                    blank_start = i
                    while i < n and not lines[i].strip():
                        i += 1
                    if i < n and _INDENT_CODE_RE.match(lines[i]):
                        code_lines.extend([""] * (i - blank_start))
                    else:
                        i = blank_start
                        break
                else:
                    break
            blocks.append(CodeBlock(language="", text="\n".join(code_lines)))
            continue

        # 顶格代码块：以 def/class/import/from import/if __name__/async def/@decorator 开头
        # 收集该行及后续行，直到遇到空行后明显非代码的内容
        if _CODE_START_RE.match(line):
            code_lines = [line]
            i += 1
            while i < n:
                cur = lines[i]
                if not cur.strip():
                    # 空行：向前看，如果后续行仍是代码，则保留空行继续
                    blank_start = i
                    while i < n and not lines[i].strip():
                        i += 1
                    if i < n and _is_code_continuation(lines[i]):
                        code_lines.extend([""] * (i - blank_start))
                    else:
                        i = blank_start
                        break
                elif _is_code_continuation(cur):
                    code_lines.append(cur)
                    i += 1
                else:
                    break
            blocks.append(CodeBlock(language="", text="\n".join(code_lines)))
            continue

        # 普通段落（连续非空非块行合并）
        para_lines = [line]
        i += 1
        while i < n:
            cur = lines[i]
            if not cur.strip():
                break
            if (
                _HEADING_RE.match(cur)
                or _FENCE_RE.match(cur)
                or _HR_RE.match(cur)
                or _UNORDERED_RE.match(cur)
                or _ORDERED_RE.match(cur)
                or _QUOTE_RE.match(cur)
                or _INDENT_CODE_RE.match(cur)
                or _CODE_START_RE.match(cur)
            ):
                break
            para_lines.append(cur)
            i += 1
        spans = _parse_inline("\n".join(para_lines))
        blocks.append(Paragraph(spans=spans))
    return blocks


def _parse_inline(text: str) -> list[Span]:
    """解析行内标记：粗体 / 斜体 / 行内代码 / 链接。

    采用 token 扫描法：按最早出现的标记切分，递归处理。代码片段内的内容不二次解析。
    """
    if not text:
        return []
    # 先抽取行内代码片段为占位，避免其内部被粗体/斜体/链接误解析
    code_segments: list[str] = []

    def _stash_code(match: re.Match[str]) -> str:
        code_segments.append(match.group(1))
        return f"\x00CODE{len(code_segments) - 1}\x00"

    work = _INLINE_CODE_RE.sub(_stash_code, text)
    spans = _parse_inline_recursive(work, code_segments, set())
    return spans


def _parse_inline_recursive(
    text: str, code_segments: list[str], styles: frozenset[str]
) -> list[Span]:
    """递归解析行内标记。"""
    spans: list[Span] = []
    pos = 0
    # 合并所有可能的起始标记，按位置排序处理
    patterns = [
        ("bold", _BOLD_RE),
        ("italic", _ITALIC_RE),
        ("link", _LINK_RE),
    ]
    while pos < len(text):
        earliest: tuple[int, str, re.Match[str]] | None = None
        for kind, pattern in patterns:
            match = pattern.search(text, pos)
            if match and (earliest is None or match.start() < earliest[0]):
                earliest = (match.start(), kind, match)
        # 代码占位回填
        code_placeholder_match = _CODE_PLACEHOLDER_RE.search(text, pos)
        if code_placeholder_match:
            cp_start = code_placeholder_match.start()
            if earliest is None or cp_start < earliest[0]:
                earliest = (cp_start, "code", code_placeholder_match)

        if earliest is None:
            # 剩余纯文本
            rest = text[pos:]
            if rest:
                spans.append(Span(text=_restore_code(rest, code_segments), styles=styles))
            break

        start, kind, match = earliest
        # 前导文本
        if start > pos:
            leading = text[pos:start]
            if leading:
                spans.append(Span(text=_restore_code(leading, code_segments), styles=styles))

        if kind == "code":
            idx = int(match.group(1))
            spans.append(
                Span(text=code_segments[idx], styles=styles | frozenset({"code"}))
            )
            pos = match.end()
        elif kind == "bold":
            inner = match.group(1) if match.group(1) is not None else match.group(2)
            spans.extend(
                _parse_inline_recursive(inner, code_segments, styles | frozenset({"bold"}))
            )
            pos = match.end()
        elif kind == "italic":
            inner = match.group(1) if match.group(1) is not None else match.group(2)
            spans.extend(
                _parse_inline_recursive(inner, code_segments, styles | frozenset({"italic"}))
            )
            pos = match.end()
        elif kind == "link":
            label = match.group(1)
            href = match.group(2)
            label_spans = _parse_inline_recursive(label, code_segments, styles)
            if label_spans:
                for sp in label_spans:
                    sp.href = href
                spans.extend(label_spans)
            else:
                spans.append(Span(text=href, styles=styles, href=href))
            pos = match.end()
    return spans


def _restore_code(text: str, code_segments: list[str]) -> str:
    """把代码占位符还原为实际代码文本。"""
    return _CODE_PLACEHOLDER_RE.sub(
        lambda m: code_segments[int(m.group(1))],
        text,
    )


# ---------------------------------------------------------------------------
# 飞书 post 富文本渲染
# ---------------------------------------------------------------------------

def render_feishu_post(blocks: list[Block]) -> dict:
    """把块模型渲染为飞书 post 消息的 content 结构（zh_cn 包裹）。

    返回形如：
        {"zh_cn": {"title": "", "content": [[{tag...}, ...], ...]}}

    每个块对应 content 数组中的一个"行"（tag 数组）。
    """
    content_rows: list[list[dict]] = []
    for block in blocks:
        row = _block_to_feishu_row(block)
        if row is not None:
            content_rows.append(row)
    return {"zh_cn": {"title": "", "content": content_rows}}


def _span_to_feishu_tag(span: Span) -> dict:
    styles = span.styles
    style_flags = []
    if "bold" in styles:
        style_flags.append("bold")
    if "italic" in styles:
        style_flags.append("italic")
    if span.href:
        tag: dict = {"tag": "a", "text": span.text, "href": span.href}
    elif "code" in styles:
        tag = {"tag": "text", "text": span.text, "un_escape": False, "style": ["code"]}
        return tag
    else:
        tag = {"tag": "text", "text": span.text, "un_escape": False}
    if style_flags:
        tag["style"] = style_flags
    return tag


def _spans_to_feishu_tags(spans: list[Span]) -> list[dict]:
    tags: list[dict] = []
    for span in spans:
        if not span.text and not span.href:
            continue
        tags.append(_span_to_feishu_tag(span))
    return tags


def _block_to_feishu_row(block: Block) -> list[dict] | None:
    if isinstance(block, Heading):
        tags = _spans_to_feishu_tags(block.spans)
        for tag in tags:
            existing = tag.get("style") or []
            tag["style"] = list(dict.fromkeys(["bold", *existing]))
        return tags or [{"tag": "text", "text": "", "un_escape": False}]
    if isinstance(block, Paragraph):
        tags = _spans_to_feishu_tags(block.spans)
        if not tags:
            return [{"tag": "text", "text": "", "un_escape": False}]
        # 段落内若含换行（多行合并），拆成多行
        return _split_paragraph_newlines(tags)
    if isinstance(block, Quote):
        tags = _spans_to_feishu_tags(block.spans)
        for tag in tags:
            tag["text"] = f"｜{tag.get('text', '')}"
        return tags or [{"tag": "text", "text": "｜", "un_escape": False}]
    if isinstance(block, ListItem):
        prefix = f"{block.index}. " if block.ordered else "• "
        tags = _spans_to_feishu_tags(block.spans)
        if tags:
            first = tags[0]
            first["text"] = f"{prefix}{first.get('text', '')}"
        else:
            tags = [{"tag": "text", "text": prefix, "un_escape": False}]
        return tags
    if isinstance(block, CodeBlock):
        return [
            {
                "tag": "code_block",
                "language": block.language or "",
                "text": block.text,
            }
        ]
    if isinstance(block, ThematicBreak):
        return [{"tag": "text", "text": "———", "un_escape": False}]
    if isinstance(block, TableBlock):
        # 表格降级为纯文本行
        return [{"tag": "text", "text": "\n".join(block.lines), "un_escape": False}]
    return None


def _split_paragraph_newlines(tags: list[dict]) -> list[dict]:
    """段落 spans 内若含 \n，拆成多行 tag（飞书 post 一行内不渲染换行）。"""
    out: list[dict] = []
    for tag in tags:
        text = tag.get("text", "")
        if "\n" not in text:
            out.append(tag)
            continue
        parts = text.split("\n")
        for part in parts:
            new_tag = dict(tag)
            new_tag["text"] = part
            out.append(new_tag)
    return out if out else [{"tag": "text", "text": "", "un_escape": False}]


def _detect_fence(line: str) -> tuple[str, str] | None:
    """若 line 是围栏起始/闭合行，返回 (fence_marker, language)；否则返回 None。

    language 仅在起始行有意义，闭合行时为空字符串。
    """
    match = _FENCE_RE.match(line)
    if not match:
        return None
    return match.group(1), match.group(2) or ""


def _is_fence_close(line: str, fence_marker: str) -> bool:
    """判断 line 是否是对应围栏的闭合行。"""
    fence_char = re.escape(fence_marker[0])
    return bool(re.match(rf"^\s*{fence_char}{{3,}}\s*$", line))


def _fence_overhead(fence_marker: str, lang: str) -> int:
    """计算包裹一个 chunk 所需的围栏开销（字符数）。

    形如 ``open_fence\\n content \\nfence_marker``，开销 =
    len(open_fence) + 1 (\\n) + 1 (\\n) + len(fence_marker)。
    """
    open_fence = f"{fence_marker}{lang}" if lang else fence_marker
    return len(open_fence) + 1 + 1 + len(fence_marker)


def _wrap_in_fence(content: str, fence_marker: str, lang: str) -> str:
    """将 content 包裹在围栏代码块中，返回自包含的 markdown。"""
    open_fence = f"{fence_marker}{lang}" if lang else fence_marker
    return f"{open_fence}\n{content}\n{fence_marker}"


def split_markdown_by_lines(text: str, limit: int) -> list[str]:
    """按行边界切分 markdown，保证不破坏围栏代码块且每个 chunk 长度 <= limit。

    用于富文本路径的分块：优先在空行处切分，其次在普通行边界。
    关键约束：
    1. 当切分点落在围栏代码块内部时，会在前一段末尾补上闭合围栏、
       在后一段开头重新打开同语言的围栏，使每个 chunk 都是合法且自包含的 Markdown。
    2. 每个 chunk 的长度严格不超过 limit：累积阶段 current_len 始终预留
       闭合围栏开销，围栏代码块内的硬切也预留围栏开销。
    3. 若围栏开销本身 >= limit（极小 limit 场景），降级为不包裹围栏的纯文本硬切，
       避免无限循环。
    """
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    lines = text.split("\n")
    chunks: list[str] = []
    current_lines: list[str] = []
    # current_len 跟踪当前累积内容（含未来 flush 时追加的闭合围栏）的总长度。
    # 即：若处于围栏内，current_len 已包含 "open_fence\\n ... \\n close_fence" 的完整长度。
    current_len = 0
    # 当前处于围栏代码块内时的状态：None 表示不在代码块内
    fence_state: tuple[str, str] | None = None  # (fence_marker, language)

    def _close_fence_len() -> int:
        """若处于围栏内，返回 flush 时追加的 "\\n" + close_fence 的长度。"""
        if fence_state is None:
            return 0
        return 1 + len(fence_state[0])

    def _flush() -> None:
        nonlocal current_lines, current_len
        if not current_lines:
            return
        chunk_lines = list(current_lines)
        # 若切分时处于围栏代码块内部，补上闭合围栏使本段自包含
        if fence_state is not None:
            chunk_lines.append(fence_state[0])
        chunks.append("\n".join(chunk_lines).rstrip("\n"))
        current_lines = []
        current_len = 0

    def _start_chunk_with_fence(line: str, line_len: int) -> None:
        """在新 chunk 开头重新打开围栏并放入 line（fence_state 非 None 时调用）。"""
        nonlocal current_lines, current_len
        fence_marker, lang = fence_state  # type: ignore[misc]
        open_fence = f"{fence_marker}{lang}" if lang else fence_marker
        current_lines = [open_fence, line]
        # open_fence + \n + line + \n + close_fence
        current_len = len(open_fence) + 1 + len(line) + 1 + len(fence_marker)

    for line in lines:
        line_with_newline = line + "\n"
        line_len = len(line_with_newline)

        # 检测围栏状态变化（在长度判断之前，确保 fence_state 准确）
        if fence_state is None:
            fence_open = _detect_fence(line)
        else:
            fence_open = None

        # 单行超长：先 flush 已累积内容，再硬切该行。
        if line_len > limit:
            _flush()
            remaining = line
            if fence_state is not None:
                # 围栏内的超长行：每个硬切片段都需包裹围栏，确保独立可渲染为代码块
                fence_marker, lang = fence_state
                overhead = _fence_overhead(fence_marker, lang)
                if overhead >= limit:
                    # 极小 limit：围栏开销已超限，降级为纯文本硬切（不包裹围栏）
                    while len(remaining) > limit:
                        chunks.append(remaining[:limit])
                        remaining = remaining[limit:]
                else:
                    # 预留围栏开销后切分，保证包裹后 chunk <= limit
                    content_budget = limit - overhead
                    while len(remaining) > content_budget:
                        chunk_content = remaining[:content_budget]
                        chunks.append(_wrap_in_fence(chunk_content, fence_marker, lang))
                        remaining = remaining[content_budget:]
                if remaining:
                    # 余下部分作为新 chunk 的起点，current_len 已含闭合围栏预留
                    _start_chunk_with_fence(remaining, len(remaining) + 1)
            else:
                while len(remaining) > limit:
                    chunks.append(remaining[:limit])
                    remaining = remaining[limit:]
                if remaining:
                    current_lines = [remaining]
                    current_len = len(remaining) + 1
            # 围栏状态不因普通代码行变化
            continue

        # 判断加入该行后总长是否超限。
        # current_len 已含闭合围栏预留（若处于围栏内），所以 current_len + line_len
        # 就是加入该行后 flush 的真实 chunk 长度。
        if current_len + line_len > limit:
            _flush()
            # 新段开头：若仍处于围栏代码块内，重新打开围栏
            if fence_state is not None:
                fence_marker, lang = fence_state
                open_fence = f"{fence_marker}{lang}" if lang else fence_marker
                # 重新打开围栏 + 该行 + 闭合围栏 可能超 limit：对该行硬切
                full_len = len(open_fence) + 1 + len(line) + 1 + len(fence_marker)
                if full_len > limit:
                    overhead = _fence_overhead(fence_marker, lang)
                    if overhead < limit:
                        content_budget = limit - overhead
                        remaining = line
                        while len(remaining) > content_budget:
                            chunk_content = remaining[:content_budget]
                            chunks.append(_wrap_in_fence(chunk_content, fence_marker, lang))
                            remaining = remaining[content_budget:]
                        if remaining:
                            _start_chunk_with_fence(remaining, len(remaining) + 1)
                        # 已处理该行，更新围栏状态后跳过下方累积逻辑
                        if fence_open is not None:
                            fence_state = fence_open
                        elif fence_state is not None and _is_fence_close(line, fence_state[0]):
                            fence_state = None
                        continue
                    else:
                        # 降级：不包裹围栏
                        current_lines = [line]
                        current_len = line_len
                else:
                    _start_chunk_with_fence(line, line_len)
            else:
                current_lines = [line]
                current_len = line_len
        else:
            current_lines.append(line)
            current_len += line_len

        # 更新围栏状态：
        # - 进入围栏时，current_len 需加上闭合围栏预留（\n + close_fence）
        # - 离开围栏时，current_len 需减去之前加的预留（因为该行就是闭合行，已含在 line_len 中）
        if fence_open is not None:
            fence_state = fence_open
            # 进入围栏：预留闭合围栏开销（\n + fence_marker）
            current_len += 1 + len(fence_open[0])
        elif fence_state is not None and _is_fence_close(line, fence_state[0]):
            # 离开围栏：该行就是闭合行，已被 current_len 计入（作为普通行），
            # 但之前预留的 \n + close_fence 不再需要（因为 close_fence 就是该行本身）。
            # 实际上该行作为 line_len 加入时含 \n，而 join 中 close 行前也有 \n，
            # 所以 current_len 正好。只需清除预留的额外 \n + close_fence 长度。
            # 但要小心：进入围栏时加了 (1 + len(close))，现在该行就是 close，
            # line_len = len(close) + 1，所以 current_len 多了 (1 + len(close))。
            # 需要减去 (1 + len(close))。
            current_len -= 1 + len(fence_state[0])
            fence_state = None

    _flush()
    return [c for c in chunks if c]


def extract_dingtalk_title(text: str, *, max_length: int = 20) -> str:
    """从 markdown 文本提取钉钉 markdown 消息的 title。

    规则：首个 `#` 标题文本 → 否则首个非空行 → 否则默认 "消息"；截断 ≤ max_length 字。
    """
    if not text or not text.strip():
        return "消息"
    for line in text.split("\n"):
        match = _HEADING_RE.match(line)
        if match:
            title = match.group(2).strip()
            if title:
                return title[:max_length]
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped:
            return stripped[:max_length]
    return "消息"


def ensure_code_fences(text: str) -> str:
    """为缺少围栏的代码块补上 ``` 围栏，使钉钉等原生 markdown 渲染器正确识别。

    利用 parse_markdown 的块识别能力定位代码块区间，然后检查原始文本中
    对应位置是否已有围栏；没有则插入 ``` 围栏。
    """
    if not text or not text.strip():
        return text
    blocks = parse_markdown(text)
    lines = text.split("\n")
    # 收集需要加围栏的代码块行区间 [start, end)（0-based，原始行号）
    fence_ranges: list[tuple[int, int, str]] = []
    idx = 0
    for block in blocks:
        if not isinstance(block, CodeBlock):
            continue
        # 跳过已有围栏的代码块（``` 或 ~~~ 开头）
        # 找到该代码块在原始行中的起始位置
        block_text_lines = block.text.split("\n") if block.text else []
        block_line_count = len(block_text_lines)
        # 从 idx 开始搜索代码块的起始行
        start = _find_block_start(lines, idx, block, block_text_lines)
        if start is None:
            continue
        end = start + block_line_count
        # 检查是否已有围栏（前一非空行是 ``` 或 ~~~）
        if _has_fence_before(lines, start):
            idx = end
            continue
        language = block.language or ""
        fence_ranges.append((start, end, language))
        idx = end

    if not fence_ranges:
        return text

    # 从后往前插入围栏，避免行号偏移
    result_lines = list(lines)
    for start, end, language in reversed(fence_ranges):
        fence_open = f"```{language}" if language else "```"
        # 插入闭合围栏（在 end 位置，即代码块最后一行之后）
        result_lines.insert(end, "```")
        # 插入开启围栏（在 start 位置）
        result_lines.insert(start, fence_open)
    return "\n".join(result_lines)


def _find_block_start(
    lines: list[str], from_idx: int, block: CodeBlock, block_lines: list[str]
) -> int | None:
    """在 lines 中从 from_idx 开始查找 CodeBlock 对应的起始行号。"""
    if not block_lines:
        return None
    first_code_line = block_lines[0].rstrip()
    for i in range(from_idx, len(lines)):
        if lines[i].rstrip() == first_code_line:
            # 验证后续行是否匹配
            match = True
            for j, bl in enumerate(block_lines):
                if i + j >= len(lines):
                    match = False
                    break
                if lines[i + j].rstrip() != bl.rstrip():
                    match = False
                    break
            if match:
                return i
    return None


def _has_fence_before(lines: list[str], code_start: int) -> bool:
    """检查代码块起始行之前是否已有围栏标记（``` 或 ~~~）。"""
    for i in range(code_start - 1, -1, -1):
        if not lines[i].strip():
            continue
        return bool(_FENCE_RE.match(lines[i]))
    return False
