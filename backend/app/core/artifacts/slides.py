"""AI 幻灯片生成：deck 数据模型、归一化与提示词。

放在 `core` 而不是 `api` 是因为有两个消费方：生成接口（`app.api.agent_apps`）与
PPTX 导出（`app.core.artifacts.slides_pptx`）—— 导出只关心「一份合法的 deck 长什么样」。

模型输出永远不可信，所有收敛都集中在 `sanitize_pages` / `finalize_deck`：
版式越界、字段缺失、条目超量、骨架页与勾选不一致，都在这里兜住。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

#: 生成一轮 deck 是一段较长的创作型输出（多页结构 + 每页要点），按 output_policy 给足上限，
#: 避免慢模型在「已经写了大半」时被超时判死。
SLIDES_OPERATION = "agent_app.slides"

#: 版式白名单。模型只允许从中挑，越界一律回落 ``bullets``（前端渲染器与 pptx 导出只认这几个）。
SLIDES_LAYOUTS = ("cover", "toc", "section", "bullets", "cards", "metrics", "closing")

#: 骨架页开关（前端口径）→ 版式。首页/目录页/结尾页由左侧选项决定，不计入内容页数。
SKELETON_PAGE_LAYOUTS: dict[str, str] = {"cover": "cover", "toc": "toc", "end": "closing"}
SKELETON_LAYOUTS = frozenset(SKELETON_PAGE_LAYOUTS.values())

#: 反向映射：版式 → 开关 key，用于判断「这一页是不是被勾选的骨架页」。
SKELETON_KEY_BY_LAYOUT = {layout: key for key, layout in SKELETON_PAGE_LAYOUTS.items()}

MAX_PAGES = 40
MAX_BULLETS = 6
MAX_CARDS = 6
MAX_POINTS = 4
MAX_TITLE_CHARS = 60
MAX_SUBTITLE_CHARS = 160
MAX_BODY_CHARS = 240
MAX_STYLE_PROMPT_CHARS = 4000


class SlidesCard(BaseModel):
    """一页里的一张信息卡（截图里的 01/02 卡）。"""

    badge: str = ""
    title: str = ""
    subtitle: str = ""
    tag: str = ""
    points: list[str] = Field(default_factory=list)
    callout_title: str = ""
    callout_body: str = ""


class SlidesPage(BaseModel):
    layout: str = "bullets"
    title: str = ""
    subtitle: str = ""
    label: str = ""
    bullets: list[str] = Field(default_factory=list)
    cards: list[SlidesCard] = Field(default_factory=list)


class SlidesDeck(BaseModel):
    deck_title: str = ""
    page_label: str = ""
    pages: list[SlidesPage] = Field(default_factory=list)


def text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    # 模型偶尔会把换行塞进单行字段，压平后按字数上限截断
    collapsed = " ".join(str(value).split())
    if len(collapsed) > limit:
        collapsed = collapsed[: limit - 1].rstrip() + "…"
    return collapsed


def text_list(value: Any, *, limit: int, item_chars: int) -> list[str]:
    if isinstance(value, str):
        raw_items: list[Any] = [value]
    elif isinstance(value, (list, tuple)):
        raw_items = list(value)
    else:
        raw_items = []
    items = [text(item, item_chars) for item in raw_items]
    return [item for item in items if item][:limit]


def normalize_card(raw: Any) -> SlidesCard:
    row = raw if isinstance(raw, dict) else {}
    points = text_list(row.get("points"), limit=MAX_POINTS, item_chars=MAX_BODY_CHARS)
    if not points:
        points = text_list(row.get("fields"), limit=MAX_POINTS, item_chars=MAX_BODY_CHARS)
    return SlidesCard(
        badge=text(row.get("badge") or row.get("index"), 4),
        title=text(row.get("title"), MAX_TITLE_CHARS),
        subtitle=text(row.get("subtitle"), MAX_SUBTITLE_CHARS),
        tag=text(row.get("tag") or row.get("status"), 12),
        points=points,
        callout_title=text(row.get("callout_title"), MAX_TITLE_CHARS),
        callout_body=text(row.get("callout_body"), MAX_BODY_CHARS),
    )


def normalize_page(raw: Any) -> SlidesPage:
    row = raw if isinstance(raw, dict) else {}
    layout = text(row.get("layout"), 24).lower()
    if layout not in SLIDES_LAYOUTS:
        layout = "bullets"
    raw_cards = row.get("cards") if isinstance(row.get("cards"), list) else row.get("items")
    cards = [normalize_card(card) for card in (raw_cards or [])][:MAX_CARDS]
    return SlidesPage(
        layout=layout,
        title=text(row.get("title"), MAX_TITLE_CHARS),
        subtitle=text(row.get("subtitle"), MAX_SUBTITLE_CHARS),
        label=text(row.get("label"), 24),
        bullets=text_list(row.get("bullets"), limit=MAX_BULLETS, item_chars=MAX_BODY_CHARS),
        cards=[card for card in cards if card.title or card.badge],
    )


def sanitize_pages(value: Any) -> list[SlidesPage]:
    """把模型（或前端回传）的页面数组收敛成渲染器可消费的结构。"""
    rows = value if isinstance(value, list) else []
    return [normalize_page(row) for row in rows][:MAX_PAGES]


def _fallback_cover(deck_title: str, page_label: str) -> SlidesPage:
    subtitle = page_label if page_label and page_label != deck_title else ""
    return SlidesPage(layout="cover", title=deck_title, subtitle=subtitle)


def _fallback_toc(pages: list[SlidesPage]) -> SlidesPage:
    sections = [
        SlidesCard(badge=f"{index + 1:02d}", title=page.title)
        for index, page in enumerate(page for page in pages if page.layout not in SKELETON_LAYOUTS)
    ]
    return SlidesPage(layout="toc", title="目录", cards=sections[:8])


def _fallback_closing() -> SlidesPage:
    return SlidesPage(layout="closing", title="谢谢", subtitle="")


def content_page_target(
    page_count_mode: str,
    page_count: int,
    skeleton_pages: list[str],
) -> int | None:
    """自定义模式下「内容页」的目标张数。

    页面上的「页数」是**总页数**（含骨架页），所以内容页 = 总页数 - 勾选的骨架页数。
    至少留 1 张内容页：勾满了骨架页又选很小的总页数时，宁可总页数超出，也不要出一份没内容的 deck。
    ``auto`` 模式返回 ``None``，表示交给模型按内容决定。
    """
    if page_count_mode == "auto":
        return None
    skeleton_count = len([key for key in skeleton_pages if key in SKELETON_PAGE_LAYOUTS])
    return max(1, page_count - skeleton_count)


def finalize_deck(
    raw: Any,
    *,
    template_title: str,
    page_label: str,
    skeleton_pages: list[str],
    page_count_mode: Literal["custom", "auto"] = "auto",
    page_count: int = 0,
) -> SlidesDeck:
    """把模型返回的任意 JSON 收敛成一份**与左侧选项一致**的 deck。

    选项必须双向生效：
    - 勾了某张骨架页但模型没给 → 用确定性的最小页面补上（否则用户勾了「首页」却看不到封面）；
    - 没勾的骨架页模型却给了 → 直接丢掉（没勾就不该出现）；
    - 自定义页数时内容页多于目标值 → 按顺序截断到目标页数（骨架页不计入）。
    """
    payload = raw if isinstance(raw, dict) else {}
    pages = sanitize_pages(payload.get("pages"))

    wanted = {key for key in skeleton_pages if key in SKELETON_PAGE_LAYOUTS}
    wanted_layouts = {SKELETON_PAGE_LAYOUTS[key] for key in wanted}

    # 1) 丢掉未勾选的骨架页
    pages = [page for page in pages if page.layout not in SKELETON_LAYOUTS or page.layout in wanted_layouts]

    # 2) 内容页按目标页数截断（auto 模式不限制）
    target = content_page_target(page_count_mode, page_count, skeleton_pages)
    if target is not None:
        kept: list[SlidesPage] = []
        seen_content = 0
        for page in pages:
            if page.layout in SKELETON_LAYOUTS:
                kept.append(page)
                continue
            seen_content += 1
            if seen_content <= target:
                kept.append(page)
        pages = kept

    deck_title = text(payload.get("deck_title"), MAX_TITLE_CHARS) or text(template_title, MAX_TITLE_CHARS)
    label = text(payload.get("page_label"), 24) or text(page_label, 24) or text(template_title, 24)

    # 3) 补上勾了但模型漏掉的骨架页
    present = {page.layout for page in pages}
    if "cover" in wanted_layouts and "cover" not in present:
        pages.insert(0, _fallback_cover(deck_title, label))
    if "toc" in wanted_layouts and "toc" not in present:
        toc_index = 1 if pages and pages[0].layout == "cover" else 0
        pages.insert(toc_index, _fallback_toc(pages))
    if "closing" in wanted_layouts and "closing" not in present:
        pages.append(_fallback_closing())

    if not pages:
        raise HTTPException(status_code=502, detail="模型没有返回任何页面，请重试或换一个模型")

    return SlidesDeck(deck_title=deck_title, page_label=label, pages=pages[:MAX_PAGES])


_SLIDES_SYSTEM_PROMPT_BASE = """\
你是资深演示文稿编辑。把用户给的「正文口述」整理成一套结构清晰的幻灯片，只输出 JSON。

硬性要求：
1. 只输出一个 JSON 对象，不要 Markdown 代码块、不要任何解释文字。
2. 事实（数字、专有名词、时间、责任人）只能来自用户提供的正文口述与页面文字；不要编造数据，
   也不要把示例文案当成素材。信息不足时用概括性表述，不要留空占位符。
3. 中文输出；每条 bullet 不超过 {bullet_chars} 字，标题不超过 {title_chars} 字。

JSON 结构：
{{
  "deck_title": "整套 deck 的标题",
  "page_label": "页眉右侧的短标签，如「库存收尾」（不超过 12 字）",
  "pages": [
    {{
      "layout": "cover|toc|section|bullets|cards|metrics|closing",
      "title": "本页主标题",
      "subtitle": "本页副标题/一句话结论，可为空字符串",
      "label": "本页页眉右侧短标签，可留空（留空则沿用 page_label）",
      "bullets": ["要点", "..."],
      "cards": [
        {{
          "badge": "01 或 397 这类短标记（序号/数值，最多 4 字符）",
          "title": "卡片标题",
          "subtitle": "卡片补充说明，可为空",
          "tag": "状态标签，如「已完成」，可为空",
          "points": ["对象：路秀新杰", "状态：进行中"],
          "callout_title": "右侧备注小标题，可为空",
          "callout_body": "右侧备注一句话，可为空"
        }}
      ]
    }}
  ]
}}

版式含义与用法（必须遵守）：
- cover：封面。填 title（deck 主标题）与 subtitle，cards/bullets 为空。
- toc：目录。只用 cards，每张卡的 title 是章节名，badge 是「01」「02」这样的序号。
- section：章节分隔页。title 是章节名，subtitle 一句话概括，其余为空。
- bullets：常规内容页。title + subtitle + bullets（2~5 条）。
- cards：并列信息页（最常用）。title + subtitle + 2~3 张 cards；每张卡展开一条主线，
  points 写 1~3 条「字段：值」（如「对象：…」「状态：…」），callout_title/callout_body 写右侧结论。
- metrics：数字指标页。只用 cards，badge 填数值（如「397」「20+」），title 填指标名，
  subtitle 或 points 写口径说明。
- closing：结尾页。title 是结束语，subtitle 可写下一步动作。

页数与骨架页（调用方已经给了明确口径，必须严格对齐，多一页少一页都算错）：
- 内容页只包括 section / bullets / cards / metrics 四种版式，内容页数量 = {content_pages}。
- 封面/目录/结尾由调用方的「需要额外生成的骨架页」决定：
  {skeleton_instruction}
- 位置固定：cover 在最前；toc 紧随 cover（没有 cover 时在最前）；closing 在最后。
- 页面顺序 = 正文口述的叙述顺序，把最重要的结论放在最前面。
- 骨架页不计入上面的内容页数量，整套 deck 的总页数 = 内容页 + 骨架页。
"""

_SKELETON_INSTRUCTION_WHEN_NONE = "本次不需要任何骨架页，只输出内容页。"
_SKELETON_INSTRUCTION_TEMPLATE = "本次需要生成：{labels}（{layouts}），除此之外不要生成其它骨架页。"

_SKELETON_LABELS = {
    "cover": ("封面页", "layout=cover"),
    "toc": ("目录页", "layout=toc"),
    "end": ("结尾页", "layout=closing"),
}


def build_slides_system_prompt(
    *,
    page_count_mode: str,
    page_count: int,
    skeleton_pages: list[str],
    style_prompt: str = "",
) -> str:
    wanted = [key for key in skeleton_pages if key in SKELETON_PAGE_LAYOUTS]
    if wanted:
        labels = "、".join(_SKELETON_LABELS[key][0] for key in wanted)
        layouts = "、".join(_SKELETON_LABELS[key][1] for key in wanted)
        skeleton_instruction = _SKELETON_INSTRUCTION_TEMPLATE.format(labels=labels, layouts=layouts)
    else:
        skeleton_instruction = _SKELETON_INSTRUCTION_WHEN_NONE
    target = content_page_target(page_count_mode, page_count, skeleton_pages)
    content_pages = "由你按内容自动决定（4~8 页）" if target is None else str(target)
    prompt = _SLIDES_SYSTEM_PROMPT_BASE.format(
        bullet_chars=MAX_BODY_CHARS,
        title_chars=MAX_TITLE_CHARS,
        content_pages=content_pages,
        skeleton_instruction=skeleton_instruction,
    )
    style = (style_prompt or "").strip()
    if style:
        prompt += (
            "\n\n【样式要求（来自该 Agent 的固定口径，必须遵守，优先级高于你自己的排版偏好）】\n"
            f"{style[:MAX_STYLE_PROMPT_CHARS]}\n"
        )
    return prompt
