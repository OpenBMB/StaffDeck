"""``app/core/slot_form.py``：缺槽表单描述的编译。

刻意**不**命名为 ``test_slot_form.py``——那会和 ``app/core/slot_form.py``
同名，pytest 导入期撞名会直接把收集进程干掉（exit 137），排查成本极高。
"""

from __future__ import annotations

from types import SimpleNamespace

from app.core.sop.slots.form import build_slot_form


def _edge(*comparisons: tuple[str, str, object]) -> SimpleNamespace:
    """构造一条带结构化条件的边（鸭子类型：只用到 ``.spec.comparisons``）。"""

    spec = SimpleNamespace(
        comparisons=[
            SimpleNamespace(field=field, op=op, value=value)
            for field, op, value in comparisons
        ]
    )
    return SimpleNamespace(spec=spec)


def test_empty_fields_yield_no_form() -> None:
    assert build_slot_form([]) is None
    assert build_slot_form(["", "   "]) is None


def test_control_type_inference() -> None:
    form = build_slot_form(
        [
            "employee_id",
            "include_income",
            "start_date",
            "amount",
            "arrive_at",
        ]
    )
    assert form is not None
    types = {field["name"]: field["type"] for field in form["fields"]}
    assert types["employee_id"] == "text"
    assert types["include_income"] == "boolean"
    assert types["start_date"] == "date"
    assert types["amount"] == "number"
    assert types["arrive_at"] == "datetime"


def test_boolean_field_carries_yes_no_options() -> None:
    form = build_slot_form(["is_pre_approved"])
    assert form is not None
    field = form["fields"][0]
    assert field["type"] == "boolean"
    assert [option["value"] for option in field["options"]] == [True, False]


def test_enum_values_from_edge_conditions_become_select() -> None:
    """出边条件里比较过的字面量就是用户可能给出的取值 → 下拉候选。"""

    form = build_slot_form(
        ["language"],
        edge_conditions=[
            _edge(("language", "eq", "中文")),
            _edge(("language", "in", ["英文", "中文"])),
        ],
    )
    assert form is not None
    field = form["fields"][0]
    assert field["type"] == "select"
    assert [option["value"] for option in field["options"]] == ["中文", "英文"]


def test_fuzzy_comparisons_do_not_become_options() -> None:
    """``contains`` / ``gte`` 是模糊匹配，取来当选择题会误导用户。"""

    form = build_slot_form(
        ["purpose"],
        edge_conditions=[_edge(("purpose", "contains", "报销"))],
    )
    assert form is not None
    assert form["fields"][0]["type"] == "text"
    assert "options" not in form["fields"][0]


def test_single_option_is_not_a_select() -> None:
    form = build_slot_form(
        ["language"],
        edge_conditions=[_edge(("language", "eq", "中文"))],
    )
    assert form is not None
    assert form["fields"][0]["type"] == "text"


def test_labels_come_from_skill_declaration_then_builtin() -> None:
    form = build_slot_form(
        ["employee_id", "device_model"],
        labels={"employee_id": "工号（HR 系统）"},
    )
    assert form is not None
    labels = {field["name"]: field["label"] for field in form["fields"]}
    assert labels["employee_id"] == "工号（HR 系统）"
    assert labels["device_model"] == "设备型号"


def test_unknown_field_keeps_identifier_label() -> None:
    form = build_slot_form(["some_vendor_specific_code"])
    assert form is not None
    assert form["fields"][0]["label"] == "some_vendor_specific_code"


def test_field_order_and_dedupe_follow_input() -> None:
    form = build_slot_form(["amount", "purpose", "amount"])
    assert form is not None
    assert [field["name"] for field in form["fields"]] == ["amount", "purpose"]


def test_form_carries_step_identity() -> None:
    form = build_slot_form(
        ["purpose"],
        step_name="收集证明需求信息",
        step_id="n1",
        skill_id="sop-1",
    )
    assert form is not None
    assert form["kind"] == "slot_form"
    assert form["step_name"] == "收集证明需求信息"
    assert form["step_id"] == "n1"
    assert form["skill_id"] == "sop-1"


def test_confirm_field_becomes_button_widget() -> None:
    """确认型字段（confirm_action 等）渲染成按钮，不再是让用户手打「确认」的文本框。"""

    form = build_slot_form(["confirm_action"], step_name="确认开具信息")
    assert form is not None
    field = form["fields"][0]
    assert field["type"] == "confirm"
    assert [option["value"] for option in field["options"]] == ["确认", "取消"]
    assert field["placeholder"] == ""


def test_user_confirmed_is_confirm_not_boolean() -> None:
    form = build_slot_form(["user_confirmed"])
    assert form is not None
    assert form["fields"][0]["type"] == "confirm"


def test_confirm_field_prefers_edge_condition_literals() -> None:
    """边条件里带出确认字面量（如 确认/修改）时按边条件的来。"""

    edges = [_edge(("confirm_action", "in", ["确认", "修改"]))]
    form = build_slot_form(["confirm_action"], edge_conditions=edges)
    assert form is not None
    field = form["fields"][0]
    assert field["type"] == "confirm"
    assert [option["value"] for option in field["options"]] == ["确认", "修改"]


def test_confirm_only_form_uses_step_name_as_title() -> None:
    form = build_slot_form(["confirm_action"], step_name="确认开具信息")
    assert form is not None
    assert form["title"] == "确认开具信息"


def test_mixed_form_keeps_generic_title() -> None:
    form = build_slot_form(["employee_id", "confirm_action"], step_name="确认开具信息")
    assert form is not None
    types = {field["name"]: field["type"] for field in form["fields"]}
    assert types["employee_id"] == "text"
    assert types["confirm_action"] == "confirm"
    assert form["title"] == "请补充以下信息"
