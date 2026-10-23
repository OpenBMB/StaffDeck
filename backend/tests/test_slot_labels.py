"""``app/core/slot_display.py``：槽位字段名的中文显示名。

这一层直接决定**用户看到什么**：确定性执行器（场景 A / F）是零 LLM 模板回复，
模型没有机会把 ``employee_id`` 润色成「员工工号」。所以这里既测「常见字段有没有
中文名」，也测「未知字段绝不瞎猜」这条保守边界。
"""

from __future__ import annotations

import pytest

from app.core.sop.slots.display import (
    BUILTIN_SLOT_LABELS,
    join_slot_labels,
    slot_label,
    slot_label_list,
)


def test_known_fields_render_as_chinese() -> None:
    """截图里的真实场景：这串字段以前是英文直接抛给用户。"""

    assert join_slot_labels(
        ["employee_id", "employee_name", "purpose", "language", "include_income"]
    ) == "员工工号、员工姓名、用途、语言、是否含收入项"


def test_unknown_field_falls_back_to_identifier() -> None:
    """两层都没命中就用标识符本身——宁可难看，也不要编一个错的中文名。"""

    assert slot_label("some_vendor_specific_code") == "some_vendor_specific_code"
    assert slot_label("权限") == "权限"


def test_empty_input_is_filtered() -> None:
    assert slot_label("") == ""
    assert slot_label(None) == ""
    assert slot_label_list(["employee_id", "", None]) == ["员工工号"]


def test_skill_declared_label_wins() -> None:
    labels = {"employee_id": "工号（HR 系统）"}
    assert slot_label("employee_id", labels) == "工号（HR 系统）"
    assert slot_label("employee_name", labels) == "员工姓名"


def test_skill_declared_label_tolerates_case_and_dash() -> None:
    assert slot_label("employee_id", {"Employee-ID": "工号"}) == "工号"
    assert slot_label("EMPLOYEE_ID") == "员工工号"
    assert slot_label("employee-id") == "员工工号"


def test_blank_skill_label_falls_back_to_builtin() -> None:
    """技能声明了空串/纯空格视为没声明，不能把回复搞出一个空字段名。"""

    assert slot_label("employee_id", {"employee_id": "   "}) == "员工工号"
    assert slot_label("employee_id", {"employee_id": None}) == "员工工号"


def test_duplicate_fields_are_preserved() -> None:
    """重复字段不去重：数量本身有语义（两个商品名称就是两个）。"""

    assert slot_label_list(["product_name", "product_name"]) == ["商品名称", "商品名称"]


def test_join_separator_is_configurable() -> None:
    assert join_slot_labels(["order_id", "refund_reason"], separator=" / ") == "订单号 / 退款原因"


def test_builtin_labels_are_non_empty_and_chinese() -> None:
    """词典自身的一致性：不能有空标签，也不能出现纯 ASCII 的「中文名」。"""

    for key, value in BUILTIN_SLOT_LABELS.items():
        assert key == key.strip().lower(), f"字段名应为小写无空白：{key}"
        assert value.strip(), f"{key} 的显示名为空"
        assert not value.isascii(), f"{key} 的显示名不是中文：{value}"


def test_labels_do_not_leak_into_identifier_form() -> None:
    """显示名只用于呈现：绝不能把标识符本身改掉（路由/槽位仍按原字段名走）。"""

    assert slot_label("include_income") == "是否含收入项"
    assert slot_label("include_income") != "include_income"


@pytest.mark.parametrize(
    "field,expected",
    [
        ("order_id", "订单号"),
        ("refund_reason", "退款原因"),
        ("system", "目标系统"),
        ("access_level", "访问级别"),
        ("overtime_duration", "加班时长"),
        ("unified_social_credit_code", "统一社会信用代码"),
    ],
)
def test_representative_labels(field: str, expected: str) -> None:
    assert slot_label(field) == expected
