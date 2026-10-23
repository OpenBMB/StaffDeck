"""缺槽询问的「表单描述」（A2UI 的最小实现）。

用户补信息走的一直是自然语言：模板回复问一句「请提供员工工号、用途」，
用户回一段文字，再由 ``_extract_slots_llm`` 把文字解析回结构化槽位。这条链路
有两个毛病：

1. **不准**：口语化表达（「就那个报销用的」）抽不出值，或者抽错；
2. **不友好**：用户得自己猜要写什么格式，字段名是 ``include_income`` 这种
   标识符时更没法猜。

A2UI 把一个**表单描述**随助手消息一起下发，前端渲染成原生表单控件；用户
提交的是**结构化 JSON**，后端直接写槽，那一轮槽位抽取的 LLM 调用完全省掉。

本模块只负责「把缺失字段编译成表单 schema」，纯函数、无 IO、不碰数据库：

- **字段中文名** 复用 :mod:`app.core.sop.slots.display`（技能自报 → 内置词典 → 原样）；
- **控件类型** 由字段名推断（``is_*`` → 开关、``*_date`` → 日期、
  ``*_amount`` → 数字…），推断不出来就是文本框；
- **下拉选项** 只从**图上已有的证据**里取：出边条件里与该字段比较过的字面量
  （``slot_compare`` 的 ``value``）以及工具 ``input_schema.enum``。**绝不凭空
  编选项**——猜错的选项比让用户自己打字更糟。

推断错了的代价是「控件不趁手」，不会影响任何判定逻辑：槽位写入、路由证据、
trace 里的字段名与取值口径完全不变。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence

from app.core.sop.slots.display import slot_label

SCHEMA_KIND = "slot_form"

# 控件类型（前端按此渲染）
TYPE_TEXT = "text"
TYPE_NUMBER = "number"
TYPE_DATE = "date"
TYPE_TIME = "time"
TYPE_DATETIME = "datetime"
TYPE_BOOLEAN = "boolean"
TYPE_SELECT = "select"
TYPE_CONFIRM = "confirm"

# 确认型字段的默认按钮（边条件里带出字面量时优先用边条件的）
CONFIRM_DEFAULT_OPTIONS = [
    {"value": "确认", "label": "确认"},
    {"value": "取消", "label": "取消"},
]

_MAX_TEXT_LENGTH = 200
_MAX_SELECT_OPTIONS = 30

# 布尔语义字段：这些前缀/后缀命中的字段，用户回答基本只有「是/否」两种
_BOOLEAN_PREFIXES = ("is_", "has_", "include_", "need_", "enable_", "allow_")
_BOOLEAN_SUFFIXES = (
    "_included",
    "_confirmed",
    "_approved",
    "_required",
    "_flag",
    "_enabled",
)

_DATE_SUFFIXES = ("_date", "_day", "_deadline")
_TIME_SUFFIXES = ("_time", "_clock")
_DATETIME_SUFFIXES = ("_at", "_datetime", "_timestamp")

_NUMBER_SUFFIXES = (
    "_amount",
    "amount",
    "_count",
    "count",
    "_quantity",
    "quantity",
    "_days",
    "_hours",
    "_minutes",
    "_num",
    "_number",
    "_price",
    "_size",
    "_total",
    "duration",
)

_TOKEN_SPLIT = re.compile(r"[^0-9a-z]+")


def build_slot_form(
    missing: Iterable[Any],
    *,
    labels: Mapping[str, Any] | None = None,
    edge_conditions: Sequence[Any] | None = None,
    step_name: str = "",
    step_id: str = "",
    skill_id: str = "",
    title: str = "",
) -> dict[str, Any] | None:
    """把缺失字段编译成表单描述；没有可问字段时返回 ``None``。

    ``edge_conditions`` 是 ``sop_step_executor._EdgeCondition`` 列表（鸭子类型：
    只用到 ``.spec``），用于推出下拉选项；传 ``None`` 就全部退化成文本框。
    """

    names: list[str] = []
    for item in missing or []:
        text = str(item or "").strip()
        if text and text not in names:
            names.append(text)
    if not names:
        return None

    option_pool = _options_by_field(edge_conditions)
    fields: list[dict[str, Any]] = []
    for name in names:
        label = slot_label(name, labels)
        options = option_pool.get(name) or []
        control = _control_for(name, options)
        field: dict[str, Any] = {
            "name": name,
            "label": label,
            "type": control,
            "required": True,
            "placeholder": _placeholder(control, label),
        }
        if control == TYPE_SELECT:
            field["options"] = [{"value": item, "label": item} for item in options]
        elif control == TYPE_BOOLEAN:
            # 布尔控件固定两项，前端渲染成二选一（不落成自由文本）
            field["options"] = [
                {"value": True, "label": "是"},
                {"value": False, "label": "否"},
            ]
        elif control == TYPE_CONFIRM:
            # 确认控件渲染成按钮（点了就提交），选项优先取边条件字面量
            # （如「确认/修改」），没有才用默认的确认/取消。
            field["options"] = (
                [{"value": item, "label": item} for item in options]
                if len(options) >= 2
                else [dict(item) for item in CONFIRM_DEFAULT_OPTIONS]
            )
        fields.append(field)

    # 全部字段都是确认型 → 这是一个「确认步骤」，标题直接用步骤名
    # （「确认开具信息」比「请补充以下信息」更像一句问话），前端也会把
    # 底部的通用提交按钮换成按钮直提。
    if fields and all(field["type"] == TYPE_CONFIRM for field in fields):
        title = step_name.strip() or "请确认"
    else:
        title = title or "请补充以下信息"
    return {
        "kind": SCHEMA_KIND,
        "skill_id": skill_id,
        "step_id": step_id,
        "step_name": step_name,
        "title": title,
        "submit_label": "提交",
        "fields": fields,
    }


def _control_for(name: str, options: list[str]) -> str:
    """字段名 + 已有选项 → 控件类型（保守优先，命中不了就是文本框）。"""

    token = _normalize(name)
    # 确认型字段（confirm_action / user_confirmed / reconfirm…）：这类「步骤
    # 的全部意义就是让用户点一下确认」，渲染成文本框等于让用户手打「确认」
    # 二字——实测最傻的交互，全局按按钮处理。
    if "confirm" in token:
        return TYPE_CONFIRM
    if len(options) >= 2:
        return TYPE_SELECT
    if not token:
        return TYPE_TEXT
    if token.startswith(_BOOLEAN_PREFIXES) or token.endswith(_BOOLEAN_SUFFIXES):
        return TYPE_BOOLEAN
    if token.endswith(_DATETIME_SUFFIXES):
        return TYPE_DATETIME
    if token.endswith(_TIME_SUFFIXES):
        return TYPE_TIME
    if token.endswith(_DATE_SUFFIXES):
        return TYPE_DATE
    if token.endswith(_NUMBER_SUFFIXES):
        return TYPE_NUMBER
    return TYPE_TEXT


def _placeholder(control: str, label: str) -> str:
    if control in (TYPE_SELECT, TYPE_CONFIRM):
        return ""
    if control == TYPE_BOOLEAN:
        return ""
    return f"请输入{label}"


def _options_by_field(
    edge_conditions: Sequence[Any] | None,
) -> dict[str, list[str]]:
    """从出边条件里收集「某个字段被比较过的字面量」，作为下拉候选。

    只认 ``slot_compare`` 的 ``eq`` / ``in``：这两种算子后面的值就是用户实际
    可能给出的取值。``contains`` / ``gte`` 这类是模糊匹配，取出来当选择题会
    误导用户，一律不取。
    """

    pool: dict[str, list[str]] = {}
    for edge in edge_conditions or []:
        spec = getattr(edge, "spec", None)
        comparisons = getattr(spec, "comparisons", None)
        if not comparisons:
            continue
        for comparison in comparisons:
            field = str(getattr(comparison, "field", "") or "").strip()
            op = str(getattr(comparison, "op", "") or "").strip()
            if not field or op not in {"eq", "in"}:
                continue
            for value in _literal_values(getattr(comparison, "value", None)):
                bucket = pool.setdefault(field, [])
                if value not in bucket and len(bucket) < _MAX_SELECT_OPTIONS:
                    bucket.append(value)
    return pool


def _literal_values(value: Any) -> list[str]:
    """比较值 → 可展示的选项文本（只认短字符串，长句/数字不当选项）。"""

    raw: list[Any] = []
    if isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = [value]
    result: list[str] = []
    for item in raw:
        text = str(item).strip()
        if not text or len(text) > _MAX_TEXT_LENGTH:
            continue
        result.append(text)
    return result


def _normalize(name: Any) -> str:
    """字段名归一：小写 + 非字母数字转下划线单分隔（``Employee-ID`` → ``employee_id``）。"""

    tokens = [token for token in _TOKEN_SPLIT.split(str(name or "").lower()) if token]
    return "_".join(tokens)


def is_confirm_field(name: Any) -> bool:
    """字段名是否为确认语义（``confirm_action`` / ``user_confirmed`` / …）。"""

    return "confirm" in _normalize(name)


__all__ = [
    "CONFIRM_DEFAULT_OPTIONS",
    "SCHEMA_KIND",
    "TYPE_BOOLEAN",
    "TYPE_CONFIRM",
    "TYPE_DATE",
    "TYPE_DATETIME",
    "TYPE_NUMBER",
    "TYPE_SELECT",
    "TYPE_TEXT",
    "TYPE_TIME",
    "build_slot_form",
    "is_confirm_field",
]
