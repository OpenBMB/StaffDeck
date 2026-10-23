"""AI 幻灯片：把 `SlidesDeck` 渲染成 .pptx。

为什么不用「HTML 截图贴图」：截图出来的幻灯片在 PowerPoint 里不可编辑、也不能改文字，
而汇报场景几乎一定会被要求改两个字。所以这里用原生形状逐页重建 —— 颜色、层级、留白
尽量对齐前端的 `.sd-deck` 主题（见 `frontend-enterprise/src/pages/agentApps/slidesDeck.ts`），
但每一行文字都是真正的文本框。

坐标体系：16:9 = 13.333in × 7.5in（960pt × 540pt）。前端用 `cqw`（容器宽度百分比）标注尺寸，
换算关系是 1cqw = 9.6pt，所以 `4.4cqw` 的左右留白 ≈ 0.59in。
"""

from __future__ import annotations

from io import BytesIO
from typing import Sequence

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

from app.core.artifacts.slides import SKELETON_LAYOUTS, SLIDES_LAYOUTS, SlidesCard, SlidesDeck, SlidesPage

# ------------------------------------------------------------------ 主题

INK = "141B33"
INK2 = "5A6277"
MUTED = "8C93A8"
LINE = "E8EBF2"
ACCENT = "F26A1B"
ACCENT2 = "2F6BFF"
SOFT = "F7F8FB"
WHITE = "FFFFFF"
COVER_TOP = "0F1A33"
COVER_MID = "1C2A52"
COVER_END = "2C4079"

#: 中英文都用同一套字体，避免 PowerPoint 在中文机上按默认字体（宋体）显示。
FONT = "PingFang SC"
FONT_FALLBACK = "Microsoft YaHei"

SLIDE_W = 13.333
SLIDE_H = 7.5

PAD = 0.60
CONTENT_W = SLIDE_W - PAD * 2

HEADER_TOP = 0.34
HEADER_H = 0.26
HEADER_LINE_Y = 0.70

TITLE_TOP = 0.86
TITLE_H = 0.62
SUB_TOP = 1.50
SUB_H = 0.36
RULE_TOP = 1.96
RULE_W = 0.58
RULE_H = 0.055
BODY_TOP = 2.22
BODY_BOTTOM_MARGIN = 0.50
BODY_H = SLIDE_H - BODY_TOP - BODY_BOTTOM_MARGIN

# 字号（pt），由前端 cqw 换算后按中文可读性取整
SZ_HEADER = 10
SZ_TITLE = 28
SZ_SUB = 12
SZ_SECTION_NO = 14
SZ_BULLET = 14
SZ_CARD_TITLE = 16
SZ_CARD_SUB = 10
SZ_CARD_BADGE = 14
SZ_TOC = 13
SZ_METRIC_VALUE = 26
SZ_METRIC_TITLE = 12
SZ_COVER_TITLE = 36
SZ_COVER_SUB = 13
SZ_COVER_META = 9


def _rgb(value: str) -> RGBColor:
    return RGBColor.from_string(value)


def _style(run, *, size: float, bold: bool = False, color: str = INK, italic: bool = False) -> None:
    font = run.font
    font.size = Pt(size)
    font.bold = bold
    font.italic = italic
    font.color.rgb = _rgb(color)
    font.name = FONT
    # `font.name` 只写 <a:latin>；中文走 <a:ea>，不补的话 PowerPoint 会用系统默认中文字体。
    rpr = run._r.get_or_add_rPr()
    for tag in ("a:ea", "a:cs"):
        element = rpr.find(qn(tag))
        if element is None:
            element = rpr.makeelement(qn(tag), {})
            rpr.insert_element_before(
                element, "a:sym", "a:hlinkClick", "a:hlinkMouseOver", "a:rtl", "a:extLst"
            )
        element.set("typeface", FONT)


def _textbox(slide, left: float, top: float, width: float, height: float):
    box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    frame = box.text_frame
    frame.word_wrap = True
    frame.margin_left = frame.margin_right = frame.margin_top = frame.margin_bottom = 0
    return frame


def _line(
    frame,
    first: bool,
    segments: Sequence[tuple[str, dict]] | str,
    *,
    size: float,
    color: str = INK,
    bold: bool = False,
    align=PP_ALIGN.LEFT,
    spacing: float = 1.2,
    space_after: float = 0,
):
    paragraph = frame.paragraphs[0] if first else frame.add_paragraph()
    paragraph.alignment = align
    paragraph.line_spacing = spacing
    paragraph.space_after = Pt(space_after)
    items = [(segments, {})] if isinstance(segments, str) else segments
    for text, override in items:
        run = paragraph.add_run()
        run.text = text or ""
        _style(
            run,
            size=override.get("size", size),
            bold=override.get("bold", bold),
            color=override.get("color", color),
        )
    return paragraph


def _fill(shape, color: str | None) -> None:
    if color is None:
        shape.fill.background()
    else:
        shape.fill.solid()
        shape.fill.fore_color.rgb = _rgb(color)


def _outline(shape, color: str | None, width: float = 0.75) -> None:
    if color is None:
        shape.line.fill.background()
        return
    shape.line.color.rgb = _rgb(color)
    shape.line.width = Pt(width)


def _rect(
    slide,
    left: float,
    top: float,
    width: float,
    height: float,
    *,
    fill: str | None = SOFT,
    line: str | None = LINE,
    line_width: float = 0.75,
    rounded: bool = True,
    radius: float = 0.09,
):
    shape_type = MSO_SHAPE.ROUNDED_RECTANGLE if rounded else MSO_SHAPE.RECTANGLE
    shape = slide.shapes.add_shape(
        shape_type, Inches(left), Inches(top), Inches(width), Inches(height)
    )
    if rounded:
        # python-pptx 的圆角调整值是「相对短边一半」的比例，0.09 视觉上接近前端的 12px。
        shape.adjustments[0] = radius
    _fill(shape, fill)
    _outline(shape, line, line_width)
    shape.shadow.inherit = False
    return shape


def _gradient(shape, stops: Sequence[tuple[float, str]], angle: float) -> None:
    """两段以上渐变；python-pptx 的 gradient API 只给两个 stop，多了就退化成一刀切。"""
    try:
        shape.fill.gradient()
        gradient_stops = shape.fill.gradient_stops
        head, tail = stops[0], stops[-1]
        gradient_stops[0].position = head[0]
        gradient_stops[0].color.rgb = _rgb(head[1])
        gradient_stops[1].position = tail[0]
        gradient_stops[1].color.rgb = _rgb(tail[1])
        shape.fill.gradient_angle = angle
    except Exception:  # noqa: BLE001 - 某些主题/版本不支持渐变角度，退化为纯色即可
        _fill(shape, stops[0][1])


# ------------------------------------------------------------------ 通用页头


def _page_header(slide, deck: SlidesDeck, page: SlidesPage, index: int, total: int) -> None:
    brand = deck.deck_title or "演示文稿"
    label = page.label or deck.page_label
    frame = _textbox(slide, PAD, HEADER_TOP, CONTENT_W, HEADER_H)
    _line(
        frame,
        True,
        [
            ("● ", {"color": ACCENT, "bold": True}),
            (brand, {"color": "2A3350", "bold": True}),
        ],
        size=SZ_HEADER,
        color="2A3350",
        spacing=1.0,
    )
    right = _textbox(slide, PAD, HEADER_TOP, CONTENT_W, HEADER_H)
    _line(
        right,
        True,
        [
            (f"{label} · " if label else "", {"color": MUTED}),
            ("PAGE ", {"color": MUTED}),
            (f"{index + 1:02d}", {"color": INK, "bold": True, "size": SZ_HEADER + 2}),
            (" / ", {"color": MUTED}),
            (f"{total:02d}", {"color": MUTED}),
        ],
        size=SZ_HEADER,
        color=MUTED,
        align=PP_ALIGN.RIGHT,
        spacing=1.0,
    )
    _rect(
        slide,
        PAD,
        HEADER_LINE_Y,
        CONTENT_W,
        0.010,
        fill=LINE,
        line=None,
        rounded=False,
    )


def _rule(slide, portrait: bool = False) -> None:
    _rect(slide, PAD, RULE_TOP, RULE_W, RULE_H, fill=ACCENT, line=None, rounded=True, radius=0.5)


def _title_block(
    slide,
    title: str,
    subtitle: str,
    *,
    title_size: float = SZ_TITLE,
    header: bool,
) -> None:
    """标题 + 副标题 + 橙色短横；`header=False` 时（封面/章节页）不带短横。"""
    top = TITLE_TOP if header else 0.0
    frame = _textbox(slide, PAD, top, CONTENT_W, TITLE_H)
    _line(frame, True, title, size=title_size, bold=True, spacing=1.14)
    if subtitle:
        sub_top = SUB_TOP if header else top + TITLE_H + 0.06
        sub_frame = _textbox(slide, PAD, sub_top, CONTENT_W, SUB_H)
        _line(sub_frame, True, subtitle, size=SZ_SUB, color=INK2, spacing=1.4)


# ------------------------------------------------------------------ 各类版式


def _render_cover(slide, deck: SlidesDeck, page: SlidesPage, total: int) -> None:
    backdrop = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE, 0, 0, Inches(SLIDE_W), Inches(SLIDE_H)
    )
    _gradient(backdrop, [(0.0, COVER_TOP), (0.52, COVER_MID), (1.0, COVER_END)], 128)
    _outline(backdrop, None)
    backdrop.shadow.inherit = False

    title = page.title or deck.deck_title or ""
    frame = _textbox(slide, PAD, 2.55, CONTENT_W * 0.88, 1.40)
    _line(frame, True, title, size=SZ_COVER_TITLE, bold=True, color=WHITE, spacing=1.12)
    if page.subtitle:
        sub = _textbox(slide, PAD, 4.02, CONTENT_W * 0.68, 0.62)
        _line(sub, True, page.subtitle, size=SZ_COVER_SUB, color="C6D1EA", spacing=1.45)

    _rect(slide, PAD, 4.78, 0.86, 0.06, fill=ACCENT, line=None, rounded=True, radius=0.5)

    meta = _textbox(slide, PAD, SLIDE_H - 0.72, CONTENT_W, 0.28)
    left_meta = deck.page_label or deck.deck_title or ""
    _line(
        meta,
        True,
        [
            (left_meta, {"color": "8EA0C8"}),
            ("\t", {}),
            (f"PAGE 01 / {total:02d}", {"color": "8EA0C8"}),
        ],
        size=SZ_COVER_META,
        color="8EA0C8",
        spacing=1.0,
    )


def _render_section(slide, page: SlidesPage, index: int) -> None:
    number = _textbox(slide, PAD, 2.70, CONTENT_W, 0.32)
    _line(number, True, f"{index + 1:02d}", size=SZ_SECTION_NO, bold=True, color=ACCENT, spacing=1.0)
    frame = _textbox(slide, PAD, 3.08, CONTENT_W, 0.90)
    _line(frame, True, page.title, size=SZ_TITLE + 4, bold=True, spacing=1.14)
    if page.subtitle:
        sub = _textbox(slide, PAD, 4.06, CONTENT_W * 0.8, 0.50)
        _line(sub, True, page.subtitle, size=SZ_SUB, color=INK2, spacing=1.45)
    _rect(slide, PAD, 4.72, RULE_W, RULE_H, fill=ACCENT, line=None, rounded=True, radius=0.5)


def _render_toc(slide, page: SlidesPage) -> None:
    cards = [card for card in page.cards if card.title or card.badge]
    column_w = (CONTENT_W - 0.42) / 2
    row_h = 0.62
    for position, card in enumerate(cards):
        column = position % 2
        row = position // 2
        left = PAD + column * (column_w + 0.42)
        top = BODY_TOP + row * row_h
        if top + row_h > SLIDE_H - 0.30:
            break
        frame = _textbox(slide, left, top, column_w, row_h - 0.16)
        _line(
            frame,
            True,
            [
                (f"{card.badge}  " if card.badge else "", {"color": ACCENT, "bold": True, "size": SZ_TOC - 2}),
                (card.title or card.subtitle, {"color": "2C3350"}),
            ],
            size=SZ_TOC,
            color="2C3350",
            spacing=1.3,
        )
        _rect(slide, left, top + row_h - 0.16, column_w, 0.010, fill=LINE, line=None, rounded=False)


def _render_metric_cards(slide, cards: Sequence[SlidesCard]) -> None:
    columns = min(len(cards), 4) or 1
    gap = 0.24
    card_w = (CONTENT_W - gap * (columns - 1)) / columns
    rows = (len(cards) + columns - 1) // columns
    card_h = min(1.85, (BODY_H - gap * (rows - 1)) / rows)
    for position, card in enumerate(cards):
        column = position % columns
        row = position // columns
        left = PAD + column * (card_w + gap)
        top = BODY_TOP + row * (card_h + gap)
        _rect(slide, left, top, card_w, card_h)
        value = card.badge or card.title
        label = card.title if card.badge else card.subtitle
        detail = card.callout_body or " · ".join(card.points) or card.subtitle
        value_frame = _textbox(slide, left + 0.28, top + 0.30, card_w - 0.56, 0.62)
        _line(value_frame, True, value, size=SZ_METRIC_VALUE, bold=True, color=ACCENT2, spacing=1.0)
        label_frame = _textbox(slide, left + 0.28, top + 0.98, card_w - 0.56, 0.30)
        _line(label_frame, True, label, size=SZ_METRIC_TITLE, bold=True, spacing=1.2)
        detail_frame = _textbox(slide, left + 0.28, top + 1.30, card_w - 0.56, card_h - 1.52)
        _line(detail_frame, True, detail, size=SZ_CARD_SUB, color=INK2, spacing=1.35)


def _render_bullets(slide, bullets: Sequence[str], top: float, height: float) -> None:
    if not bullets:
        return
    step = min(0.52, height / len(bullets))
    for position, bullet in enumerate(bullets):
        row_top = top + position * step
        dot_top = row_top + (step - 0.10) / 2 - 0.02
        _rect(slide, PAD + 0.03, dot_top, 0.085, 0.085, fill=ACCENT2, line=None, rounded=True, radius=0.5)
        frame = _textbox(slide, PAD + 0.24, row_top, CONTENT_W - 0.24, step)
        _line(frame, True, bullet, size=SZ_BULLET, color="2C3350", spacing=1.35)


def _render_stacked_card(slide, card: SlidesCard, top: float, height: float) -> None:
    _rect(slide, PAD, top, CONTENT_W, height)
    inner_top = top + 0.20
    note_w = 2.40 if (card.callout_title or card.callout_body) else 0.0
    badge_w = 0.54
    text_left = PAD + 0.24 + (badge_w + 0.24 if card.badge else 0.0)

    if card.badge:
        badge = _rect(slide, PAD + 0.24, inner_top, badge_w, badge_w, fill=INK, line=None, rounded=True, radius=0.22)
        _fill(badge, INK)
        badge_frame = badge.text_frame
        badge_frame.margin_left = badge_frame.margin_right = 0
        badge_frame.margin_top = badge_frame.margin_bottom = 0
        badge_frame.vertical_anchor = MSO_ANCHOR.MIDDLE
        _line(badge_frame, True, card.badge, size=SZ_CARD_BADGE, bold=True, color=WHITE, align=PP_ALIGN.CENTER, spacing=1.0)

    text_w = CONTENT_W - (text_left - PAD) - (note_w + 0.30 if note_w else 0.24)
    head = _textbox(slide, text_left, inner_top, text_w, 0.36)
    segments: list[tuple[str, dict]] = [(card.title, {"size": SZ_CARD_TITLE, "bold": True})]
    if card.tag:
        segments.append((f"   {card.tag}", {"size": SZ_CARD_SUB, "bold": True, "color": ACCENT2}))
    _line(head, True, segments, size=SZ_CARD_TITLE, spacing=1.2)

    cursor = inner_top + 0.42
    if card.subtitle:
        sub = _textbox(slide, text_left, cursor, text_w, 0.28)
        _line(sub, True, card.subtitle, size=SZ_CARD_SUB + 1, color=INK2, spacing=1.3)
        cursor += 0.30
    if card.points:
        points = _textbox(slide, text_left, cursor, text_w, height - (cursor - top) - 0.18)
        for position, point in enumerate(card.points):
            _line(points, position == 0, point, size=SZ_CARD_SUB, color=INK2, spacing=1.25, space_after=1)

    if note_w:
        note_left = PAD + CONTENT_W - 0.24 - note_w
        _rect(slide, note_left - 0.22, inner_top - 0.02, 0.010, height - 0.36, fill=LINE, line=None, rounded=False)
        note = _textbox(slide, note_left, inner_top, note_w, height - 0.36)
        first = True
        if card.callout_title:
            _line(note, True, card.callout_title, size=SZ_CARD_TITLE - 2, bold=True, spacing=1.2)
            first = False
        if card.callout_body:
            _line(note, first, card.callout_body, size=SZ_CARD_SUB, color=INK2, spacing=1.3, space_after=0)


def _render_grid_cards(slide, cards: Sequence[SlidesCard], top: float, height: float) -> None:
    columns = 3 if len(cards) >= 3 else len(cards)
    columns = max(columns, 1)
    gap = 0.26
    card_w = (CONTENT_W - gap * (columns - 1)) / columns
    rows = (len(cards) + columns - 1) // columns
    limit = (height - gap * (rows - 1)) / rows
    card_h = min(limit, max(_grid_card_height(card) for card in cards))

    for position, card in enumerate(cards):
        column = position % columns
        row = position // columns
        left = PAD + column * (card_w + gap)
        card_top = top + row * (card_h + gap)
        _rect(slide, left, card_top, card_w, card_h)
        inner_left = left + 0.22
        inner_w = card_w - 0.44
        cursor = card_top + 0.20

        if card.badge:
            badge = _rect(slide, inner_left, cursor, 0.50, 0.50, fill=INK, line=None, rounded=True, radius=0.22)
            badge_frame = badge.text_frame
            badge_frame.margin_left = badge_frame.margin_right = 0
            badge_frame.margin_top = badge_frame.margin_bottom = 0
            badge_frame.vertical_anchor = MSO_ANCHOR.MIDDLE
            _line(badge_frame, True, card.badge, size=SZ_CARD_BADGE - 1, bold=True, color=WHITE, align=PP_ALIGN.CENTER, spacing=1.0)
            cursor += 0.62

        head_segments: list[tuple[str, dict]] = [(card.title, {"size": SZ_CARD_TITLE - 1, "bold": True})]
        if card.tag:
            head_segments.append((f"   {card.tag}", {"size": SZ_CARD_SUB, "bold": True, "color": ACCENT2}))
        head = _textbox(slide, inner_left, cursor, inner_w, 0.40)
        _line(head, True, head_segments, size=SZ_CARD_TITLE - 1, spacing=1.2)
        cursor += 0.44

        if card.subtitle:
            sub = _textbox(slide, inner_left, cursor, inner_w, 0.28)
            _line(sub, True, card.subtitle, size=SZ_CARD_SUB, color=INK2, spacing=1.3)
            cursor += 0.28
        if card.points:
            points = _textbox(slide, inner_left, cursor, inner_w, max(0.24, card_h - (cursor - card_top) - 0.18))
            for index, point in enumerate(card.points):
                _line(points, index == 0, point, size=SZ_CARD_SUB, color=INK2, spacing=1.25, space_after=1)


def _stacked_card_height(card: SlidesCard) -> float:
    """按内容估算卡高：与前端「卡片高度自适应」一致，不做无意义的拉伸。"""
    height = 0.40 + 0.42  # 上下内边距 + 标题行
    if card.subtitle:
        height += 0.30
    if card.points:
        height += len(card.points) * 0.26
    return max(1.00, height)


def _grid_card_height(card: SlidesCard) -> float:
    height = 0.40
    if card.badge:
        height += 0.62
    height += 0.44
    if card.subtitle:
        height += 0.28
    if card.points:
        height += len(card.points) * 0.24
    return max(1.10, height)


def _render_content(slide, deck: SlidesDeck, page: SlidesPage, index: int, total: int) -> None:
    _page_header(slide, deck, page, index, total)
    _title_block(slide, page.title, page.subtitle, header=True)
    _rule(slide)

    bullets = list(page.bullets or [])
    cards = [card for card in (page.cards or []) if card.title or card.badge]

    bullet_h = 0.0
    if bullets and cards:
        bullet_h = min(len(bullets) * 0.44, BODY_H * 0.45)
    elif bullets:
        bullet_h = BODY_H
    if bullet_h:
        _render_bullets(slide, bullets, BODY_TOP, bullet_h)

    if not cards:
        return

    cards_top = BODY_TOP + (bullet_h + 0.24 if bullet_h else 0.0)
    cards_h = SLIDE_H - BODY_BOTTOM_MARGIN - cards_top
    gap = 0.26
    if len(cards) >= 3:
        _render_grid_cards(slide, cards, cards_top, cards_h)
        return
    limit = (cards_h - gap * (len(cards) - 1)) / len(cards)
    for position, card in enumerate(cards):
        height = min(limit, _stacked_card_height(card))
        _render_stacked_card(slide, card, cards_top + position * (height + gap), height)


def _render_page(slide, deck: SlidesDeck, page: SlidesPage, index: int, total: int) -> None:
    layout = page.layout if page.layout in SLIDES_LAYOUTS else "bullets"
    if layout == "cover":
        _render_cover(slide, deck, page, total)
    elif layout == "toc":
        _page_header(slide, deck, page, index, total)
        _title_block(slide, page.title or "目录", page.subtitle, header=True)
        _rule(slide)
        _render_toc(slide, page)
    elif layout == "section":
        _page_header(slide, deck, page, index, total)
        _render_section(slide, page, index)
    elif layout == "closing":
        _page_header(slide, deck, page, index, total)
        frame = _textbox(slide, PAD, 2.70, CONTENT_W, 1.00)
        _line(frame, True, page.title or "谢谢", size=SZ_TITLE + 6, bold=True, spacing=1.14)
        if page.subtitle:
            sub = _textbox(slide, PAD, 3.82, CONTENT_W * 0.8, 0.50)
            _line(sub, True, page.subtitle, size=SZ_SUB, color=INK2, spacing=1.45)
    elif layout == "metrics":
        _page_header(slide, deck, page, index, total)
        _title_block(slide, page.title, page.subtitle, header=True)
        _rule(slide)
        _render_metric_cards(slide, [card for card in (page.cards or []) if card.title or card.badge])
    else:
        _render_content(slide, deck, page, index, total)


def build_slides_pptx(deck: SlidesDeck) -> bytes:
    """把一份（已收敛的）deck 渲染成 pptx 字节流。"""
    presentation = Presentation()
    presentation.slide_width = Inches(SLIDE_W)
    presentation.slide_height = Inches(SLIDE_H)
    blank_layout = presentation.slide_layouts[6]

    pages: list[SlidesPage] = list(deck.pages or [])
    total = len(pages)
    for index, page in enumerate(pages):
        slide = presentation.slides.add_slide(blank_layout)
        _render_page(slide, deck, page, index, total)

    if total == 0:  # 兜底，正常路径有 400 挡在前面
        presentation.slides.add_slide(blank_layout)

    buffer = BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


#: 供测试断言「骨架页版式」用，避免测试里再硬编码一遍。
SKELETON_LAYOUTS_REF = SKELETON_LAYOUTS
