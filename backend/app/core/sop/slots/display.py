"""槽位字段名的「用户可见中文名」。

``expected_user_info`` / ``required_info`` 里的条目是建模时产生的字段标识符
（``employee_id`` / ``include_income``…），确定性执行器把它们直接拼进回复就
变成了：:

    为了继续「收集证明需求信息」，请提供：employee_id、employee_name、purpose。

对用户而言这串英文没有任何可读性——尤其是场景 A / F 这类**零 LLM** 的模板回复，
模型没有机会把它润色成中文。本模块给字段名一个中文显示名，来源分三层：

1. **技能自报**：``content_json.slot_labels = {"employee_id": "员工工号"}``。
   作者最清楚字段的业务含义，优先级最高；
2. **内置词典**：覆盖演示样例与内置种子技能里出现的全部字段（见
   :data:`BUILTIN_SLOT_LABELS`），新技能只要字段名是常见语义即可直接命中；
3. **原样返回**：两层都没命中就用标识符本身。**绝不猜测**——宁可让用户看到
   ``device_model``，也不要编一个错的中文名去误导他。

这一层只影响**呈现**：路由证据、槽位写入、观测事件里的字段名一律保持原始
标识符，改显示名不会动摇任何判定逻辑（``sop_step_executor`` 只在拼回复时用）。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

# 内置词典：字段标识符 → 中文显示名。
# 覆盖范围 = 演示样例 + 内置种子技能 + 常见业务语义；新增字段只需往这里补一行。
BUILTIN_SLOT_LABELS: dict[str, str] = {
    # 人事 / 证明
    "employee_id": "员工工号",
    "employee_name": "员工姓名",
    "cert_type": "证明类型",
    "purpose": "用途",
    "language": "语言",
    "include_income": "是否含收入项",
    "unified_social_credit_code": "统一社会信用代码",
    "company_name": "公司名称",
    "seal_type": "印章类型",
    "seal_purpose": "用章用途",
    "is_pre_approved": "是否已预审批",
    # 权限
    "system": "目标系统",
    "permission": "权限级别",
    "access_level": "访问级别",
    # 审批 / 合同
    "contract_type": "合同类型",
    "contract_content": "合同内容",
    "clause_content": "条款内容",
    "modification_request": "修改要求",
    "priority": "优先级",
    # 报销 / 发票
    "invoice_number": "发票号码",
    "invoice_code": "发票代码",
    "invoice_amount": "发票金额",
    "invoice_date": "开票日期",
    "over_limit_amount": "超限金额",
    "over_limit_reason": "超限原因",
    # 请假 / 加班
    "leave_type": "请假类型",
    "start_date": "开始日期",
    "end_date": "结束日期",
    "start_time": "开始时间",
    "end_time": "结束时间",
    "overtime_date": "加班日期",
    "overtime_duration": "加班时长",
    "overtime_reason": "加班事由",
    # 出行 / 会议
    "trip_info": "出差信息",
    "attendees": "参会人员",
    # 报修
    "fault_phenomenon": "故障现象",
    "impact_range": "影响范围",
    # 采购 / 退换货
    "order_id": "订单号",
    "order_confirmed": "订单确认",
    "refund_type": "退款类型",
    "refund_reason": "退款原因",
    "exchange_type": "换货类型",
    "exchange_reason": "换货原因",
    "product_id": "商品编号",
    "product_name": "商品名称",
    "product_name_1": "第一个商品名称",
    "product_name_2": "第二个商品名称",
    "quantity": "数量",
    "user_name": "用户姓名",
    "purchase_confirmed": "购买确认",
    # 通用
    "amount": "金额",
    "category": "类别",
    "date": "日期",
    "month": "月份",
    "title": "标题",
    "description": "说明",
    "reason": "事由",
    "contact": "联系方式",
    "items": "物品清单",
    "document_name": "文件名称",
    "confirm_action": "确认操作",
    "confirm_submission": "提交确认",
    "confirmation": "确认",
    "request_type": "申请类型",
    "device_model": "设备型号",
    "urgency": "紧急程度",
}

# 归一化索引：容忍 Employee_ID / employee-id 这类写法差异
_NORMALIZED_BUILTIN: dict[str, str] = {
    key.strip().lower().replace("-", "_"): value for key, value in BUILTIN_SLOT_LABELS.items()
}


def _normalize(field: str) -> str:
    return field.strip().lower().replace("-", "_")


def _lookup(source: Mapping[str, Any] | None, field: str) -> str:
    if not isinstance(source, Mapping):
        return ""
    raw = source.get(field)
    if raw is None:
        return ""
    return str(raw).strip()


def slot_label(field: Any, labels: Mapping[str, Any] | None = None) -> str:
    """返回单个字段的显示名；未知字段**原样返回**标识符。"""

    name = str(field if field is not None else "").strip()
    if not name:
        return ""

    override = _lookup(labels, name)
    if override:
        return override
    if isinstance(labels, Mapping):
        # 作者侧也容忍大小写/连字符差异
        for key, value in labels.items():
            # 必须先排掉 None：str(None) == "None"，会把一个空标签变成
            # 字面量「None」显示给用户
            if value is None:
                continue
            if _normalize(str(key)) == _normalize(name):
                text = str(value).strip()
                if text:
                    return text

    return _NORMALIZED_BUILTIN.get(_normalize(name), name)


def slot_label_list(
    fields: Iterable[Any],
    labels: Mapping[str, Any] | None = None,
) -> list[str]:
    """批量取显示名，保持入参顺序、去掉空项（重复字段不去重：数量本身有语义）。"""

    result: list[str] = []
    for field in fields or []:
        text = slot_label(field, labels)
        if text:
            result.append(text)
    return result


def join_slot_labels(
    fields: Iterable[Any],
    labels: Mapping[str, Any] | None = None,
    *,
    separator: str = "、",
) -> str:
    return separator.join(slot_label_list(fields, labels))


__all__ = [
    "BUILTIN_SLOT_LABELS",
    "join_slot_labels",
    "slot_label",
    "slot_label_list",
]
