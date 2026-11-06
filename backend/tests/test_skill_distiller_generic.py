import json

from app.skills.skill_distiller import SkillDistiller
from app.skills.skill_schema import SkillDistillRequest


def _normalize(raw: dict, request: SkillDistillRequest):
    return SkillDistiller()._normalize_response(_graph_raw(raw), request)  # noqa: SLF001


def _graph_raw(raw: dict) -> dict:
    converted = dict(raw)
    draft = converted.get("draft_skill") if isinstance(converted.get("draft_skill"), dict) else None
    if draft is None:
        return converted
    draft = dict(draft)
    legacy_steps = draft.pop("steps", None)
    if isinstance(legacy_steps, list):
        nodes = []
        for index, step in enumerate(legacy_steps):
            if not isinstance(step, dict):
                continue
            node_id = str(step.get("step_id") or step.get("node_id") or f"node_{index + 1}")
            actions = [str(action) for action in step.get("allowed_actions", [])]
            nodes.append(
                {
                    "node_id": node_id,
                    "type": "tool_call" if any(action.startswith("call_tool:") for action in actions) else "collect_info",
                    "name": step.get("name") or node_id,
                    "instruction": step.get("instruction") or "",
                    "expected_user_info": step.get("expected_user_info") or [],
                    "allowed_actions": actions,
                }
            )
        draft["nodes"] = nodes
        draft["edges"] = [
            {
                "source_node_id": nodes[index]["node_id"],
                "next_node_id": nodes[index + 1]["node_id"],
                "priority": index,
                "label": "默认推进",
            }
            for index in range(len(nodes) - 1)
        ]
        if nodes:
            draft["start_node_id"] = nodes[0]["node_id"]
            draft["terminal_node_ids"] = [nodes[-1]["node_id"]]
    converted["draft_skill"] = draft
    return converted


def test_fallback_card_is_not_domain_hardcoded_for_commerce_text() -> None:
    request = SkillDistillRequest(
        tenant_id="tenant_demo",
        title="购买商品",
        raw_content="获取用户姓名，查询商品是否存在，生成对应订单号，反馈给用户",
        available_tools=[
            {"name": "product.purchase", "input_schema": {"required": ["product_id"]}, "requires_confirmation": True},
            {"name": "order.add", "input_schema": {"required": ["product_id"]}, "requires_confirmation": True},
        ],
    )

    card = SkillDistiller()._fallback_card(request)  # noqa: SLF001

    assert card.skill_id != "purchase_product"
    assert card.required_info == []
    assert all("operation_confirmed" not in node.expected_user_info for node in card.nodes)
    assert all(
        not any(action.startswith("call_tool:") for action in node.allowed_actions)
        for node in card.nodes
    )


def test_model_input_uses_plain_text_and_compacts_available_tools() -> None:
    request = SkillDistillRequest(
        tenant_id="tenant_demo",
        title="新SOP",
        raw_content="差旅报销申请，收集事由和金额后提交审批。",
        available_tools=[
            {
                "id": "tool_internal_id",
                "name": "expense.submit",
                "display_name": "提交报销单",
                "description": "提交差旅报销申请。",
                "method": "POST",
                "url": "http://localhost:5173/api/mock/expense/submit",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "reason": {"type": "string", "description": "报销事由"},
                        "amount": {"type": "number", "description": "报销金额"},
                    },
                    "required": ["reason", "amount"],
                },
                "output_schema": {
                    "type": "object",
                    "properties": {"request_id": {"type": "string"}},
                },
            }
        ],
    )
    distiller = SkillDistiller()

    payload = distiller._payload(request)  # noqa: SLF001
    model_input = distiller._model_input(request, payload)  # noqa: SLF001

    projected_tool = payload["available_tools"][0]
    assert set(projected_tool) == {
        "id",
        "name",
        "display_name",
        "description",
        "input_schema",
    }
    assert "output_schema" not in json.dumps(payload, ensure_ascii=False)
    assert "ID=tool_internal_id" in model_input
    assert "localhost:5173" not in model_input
    assert model_input.startswith("技能标题：新SOP\n原始流程：")
    assert "expense.submit（提交报销单）" in model_input
    assert "reason (string, 必填)" in model_input
    assert not model_input.lstrip().startswith("{")


def test_distill_preserves_and_resolves_capability_references() -> None:
    request = SkillDistillRequest(
        tenant_id="tenant_demo",
        title="每日科技新闻入 Excel",
        raw_content="获取每日科技新闻，整理后写入 Excel。",
        available_tools=[
            {
                "id": "tool_news",
                "name": "news.fetch",
                "display_name": "科技新闻获取",
                "description": "获取当天科技新闻。",
            },
            {
                "id": "tool_excel",
                "name": "excel.write",
                "display_name": "Excel 写入",
                "description": "把结构化数据写入 Excel。",
            },
        ],
        available_general_skills=[
            {
                "id": "genskill_news",
                "slug": "news-summary",
                "name": "科技新闻整理",
                "description": "筛选并整理科技新闻。",
            }
        ],
        available_knowledge_bases=[
            {
                "id": "kb_news_rules",
                "name": "新闻筛选规范",
                "description": "科技新闻筛选标准。",
            }
        ],
    )
    raw = {
        "draft_skill": {
            "skill_id": "daily_tech_news_excel",
            "name": "每日科技新闻入 Excel",
            "nodes": [
                {
                    "node_id": "collect_news",
                    "type": "tool_call",
                    "name": "获取并整理新闻",
                    "instruction": "获取科技新闻并按规范整理。",
                    "allowed_actions": ["call_tool:news.fetch"],
                    "capability_refs": {
                        "general_skill_ids": ["general_skill.news-summary"],
                        "required_general_skill_ids": ["genskill_news"],
                        "tool_ids": ["news.fetch"],
                        "required_tool_ids": ["tool_news"],
                        "knowledge_base_ids": ["新闻筛选规范"],
                    },
                },
                {
                    "node_id": "write_excel",
                    "type": "tool_call",
                    "name": "写入 Excel",
                    "instruction": "将整理结果写入 Excel。",
                    "allowed_actions": ["call_tool:excel.write"],
                    "capability_refs": {},
                },
            ],
            "edges": [
                {
                    "source_node_id": "collect_news",
                    "next_node_id": "write_excel",
                    "priority": 0,
                }
            ],
            "start_node_id": "collect_news",
            "terminal_node_ids": ["write_excel"],
        }
    }

    response = SkillDistiller()._normalize_response(raw, request)  # noqa: SLF001

    collect_refs = response.draft_skill.nodes[0].capability_refs
    assert collect_refs.general_skill_ids == ["genskill_news"]
    assert collect_refs.required_general_skill_ids == ["genskill_news"]
    assert collect_refs.tool_ids == ["tool_news"]
    assert collect_refs.required_tool_ids == ["tool_news"]
    assert collect_refs.knowledge_base_ids == ["kb_news_rules"]
    assert response.draft_skill.nodes[1].capability_refs.tool_ids == ["tool_excel"]
    assert not any("不可用的能力" in warning for warning in response.warnings)

    model_input = SkillDistiller()._model_input(request)  # noqa: SLF001
    assert "ID=tool_news" in model_input
    assert "ID=tool_excel" in model_input
    assert "ID=genskill_news" in model_input
    assert "ID=kb_news_rules" in model_input


def test_distill_infers_explicit_catalog_capabilities_when_model_omits_refs() -> None:
    request = SkillDistillRequest(
        tenant_id="tenant_demo",
        title="在职证明开具",
        raw_content=(
            "必须使用证明文书生成技能，先检索员工证明规范知识库，"
            "再必须调用 hr.cert_issue 工具正式开具证明。"
        ),
        available_tools=[
            {
                "id": "tool_cert_issue",
                "name": "hr.cert_issue",
                "display_name": "在职收入证明开具",
            }
        ],
        available_general_skills=[
            {
                "id": "genskill_proof",
                "slug": "document-generation-for-proofs",
                "name": "证明文书生成",
            }
        ],
        available_knowledge_bases=[
            {
                "id": "kb_proof_rules",
                "name": "员工证明规范知识库",
            }
        ],
    )
    raw = {
        "draft_skill": {
            "skill_id": "employee_cert_issue",
            "name": "在职证明开具",
            "nodes": [
                {
                    "node_id": "search_rules",
                    "type": "knowledge_search",
                    "name": "检索证明规范",
                    "instruction": "检索证明规范后准备开具参数。",
                    "allowed_actions": ["continue_flow"],
                },
                {
                    "node_id": "issue_cert",
                    "type": "tool_call",
                    "name": "调用 hr.cert_issue 开具证明",
                    "instruction": "调用工具正式开具证明。",
                    "allowed_actions": ["call_tool:hr.cert_issue"],
                },
            ],
            "edges": [
                {
                    "source_node_id": "search_rules",
                    "next_node_id": "issue_cert",
                }
            ],
            "start_node_id": "search_rules",
            "terminal_node_ids": ["issue_cert"],
        }
    }

    response = SkillDistiller()._normalize_response(raw, request)  # noqa: SLF001

    search_refs = response.draft_skill.nodes[0].capability_refs
    issue_refs = response.draft_skill.nodes[1].capability_refs
    assert search_refs.knowledge_base_ids == ["kb_proof_rules"]
    assert issue_refs.general_skill_ids == ["genskill_proof"]
    assert issue_refs.required_general_skill_ids == ["genskill_proof"]
    assert issue_refs.tool_ids == ["tool_cert_issue"]
    assert issue_refs.required_tool_ids == ["tool_cert_issue"]


def test_slot_policy_targets_model_generated_fields() -> None:
    request = SkillDistillRequest(
        tenant_id="tenant_demo",
        title="设备报修",
        raw_content="收集设备编号和问题描述，创建维修工单",
    )
    raw = {
        "draft_skill": {
            "skill_id": "repair_ticket",
            "name": "设备报修",
            "required_info": ["asset_id"],
            "steps": [
                {
                    "step_id": "collect_repair_info",
                    "name": "收集报修信息",
                    "instruction": "同时抽取设备编号和问题描述。",
                    "expected_user_info": ["asset_id", "issue_desc"],
                    "allowed_actions": ["ask_user", "continue_flow"],
                }
            ],
        }
    }

    response = _normalize(raw, request)

    assert response.draft_skill.slot_filling_policy["target_info"] == ["asset_id", "issue_desc"]


def test_normalize_response_does_not_infer_tool_or_confirmation_from_raw_words() -> None:
    request = SkillDistillRequest(
        tenant_id="tenant_demo",
        title="退款处理",
        raw_content="获取订单号，核实订单是否符合退款条件，处理退款并反馈给用户",
        available_tools=[
            {
                "name": "order.query",
                "input_schema": {"required": ["order_id"]},
            }
        ],
    )
    raw = {
        "draft_skill": {
            "skill_id": "refund",
            "name": "退款处理",
            "required_info": ["order_id"],
            "steps": [
                {
                    "step_id": "collect_order",
                    "name": "收集订单",
                    "instruction": "收集订单号。",
                    "expected_user_info": ["order_id"],
                    "allowed_actions": ["ask_user", "continue_flow"],
                }
            ],
            "response_rules": [],
        }
    }

    response = _normalize(raw, request)
    nodes = response.draft_skill.nodes

    assert all(
        not any(action.startswith("call_tool:") for action in node.allowed_actions)
        for node in nodes
    )
    assert all("operation_confirmed" not in node.expected_user_info for node in nodes)
    assert "answer_user" in nodes[-1].allowed_actions
    assert any("不得把" in rule and "请稍候" in rule for rule in response.draft_skill.response_rules)
    assert any("自适应推进" in rule for rule in response.draft_skill.response_rules)
    assert not any("确认关键对象" in rule for rule in response.draft_skill.response_rules)
    assert all("目标而不是固定话术" in node.instruction for node in nodes)


def test_normalize_response_preserves_model_declared_tool_and_confirmation() -> None:
    request = SkillDistillRequest(
        tenant_id="tenant_demo",
        title="退款处理",
        raw_content="获取订单号，核实订单是否符合退款条件，处理退款并反馈给用户",
        available_tools=[
            {
                "name": "order.query",
                "input_schema": {"required": ["order_id"]},
            }
        ],
    )
    raw = {
        "draft_skill": {
            "skill_id": "refund",
            "name": "退款处理",
            "required_info": ["order_id"],
            "steps": [
                {
                    "step_id": "collect_order",
                    "name": "收集订单",
                    "instruction": "收集订单号。",
                    "expected_user_info": ["order_id"],
                    "allowed_actions": ["ask_user", "continue_flow"],
                },
                {
                    "step_id": "confirm_operation",
                    "name": "确认操作",
                    "instruction": "确认关键对象和操作内容。",
                    "expected_user_info": ["operation_confirmed"],
                    "allowed_actions": ["ask_user", "continue_flow"],
                },
                {
                    "step_id": "query_order",
                    "name": "查询订单",
                    "instruction": "调用工具查询订单状态。",
                    "expected_user_info": [],
                    "allowed_actions": ["continue_flow", "call_tool:order.query"],
                }
            ],
            "response_rules": [],
        }
    }

    response = _normalize(raw, request)
    nodes = response.draft_skill.nodes

    assert any("call_tool:order.query" in node.allowed_actions for node in nodes)
    confirm_index = next(
        index for index, node in enumerate(nodes) if "operation_confirmed" in node.expected_user_info
    )
    tool_index = next(
        index
        for index, node in enumerate(nodes)
        if any(action.startswith("call_tool:") for action in node.allowed_actions)
    )
    assert confirm_index < tool_index
    assert "operation_confirmed=true" in nodes[tool_index].instruction
    assert "answer_user" in nodes[-1].allowed_actions
    assert any("不得把" in rule and "请稍候" in rule for rule in response.draft_skill.response_rules)
    assert any("自适应推进" in rule for rule in response.draft_skill.response_rules)
    assert any("确认关键对象" in rule for rule in response.draft_skill.response_rules)
    assert all("目标而不是固定话术" in node.instruction for node in nodes)


def test_normalize_response_makes_duplicate_step_ids_unique() -> None:
    request = SkillDistillRequest(
        tenant_id="tenant_demo",
        title="购买商品",
        raw_content="获取用户姓名，生成订单号，反馈给用户",
    )
    raw = {
        "draft_skill": {
            "skill_id": "purchase",
            "name": "购买商品",
            "required_info": ["user_name"],
            "steps": [
                {
                    "step_id": "reply_result",
                    "name": "创建订单",
                    "instruction": "创建订单。",
                    "expected_user_info": ["user_name"],
                    "allowed_actions": ["continue_flow"],
                },
                {
                    "step_id": "reply_result",
                    "name": "反馈订单",
                    "instruction": "反馈订单结果。",
                    "expected_user_info": [],
                    "allowed_actions": ["answer_user"],
                },
            ],
        }
    }

    response = _normalize(raw, request)
    step_ids = [node.node_id for node in response.draft_skill.nodes]

    assert len(step_ids) == len(set(step_ids))
    assert "reply_result" in step_ids
    assert "reply_result_2" in step_ids
    assert any("node_id" in warning for warning in response.warnings)


def test_normalize_response_turns_steps_into_adaptive_goals() -> None:
    request = SkillDistillRequest(
        tenant_id="tenant_demo",
        title="资料审核",
        raw_content="收集姓名和资料编号，审核资料状态，反馈给用户",
    )
    raw = {
        "draft_skill": {
            "skill_id": "document_review",
            "name": "资料审核",
            "required_info": ["user_name", "document_id"],
            "steps": [
                {
                    "step_id": "collect_info",
                    "name": "收集信息",
                    "instruction": "询问用户姓名和资料编号。",
                    "expected_user_info": ["user_name", "document_id"],
                    "allowed_actions": ["ask_user", "continue_flow"],
                },
                {
                    "step_id": "reply_result",
                    "name": "反馈结果",
                    "instruction": "反馈审核结果。",
                    "expected_user_info": [],
                    "allowed_actions": ["answer_user"],
                },
            ],
            "response_rules": [],
        }
    }

    response = _normalize(raw, request)

    assert response.draft_skill.slot_filling_policy["multi_slot_per_turn"] is True
    assert response.draft_skill.slot_filling_policy["skip_satisfied_steps"] is True
    assert all("目标而不是固定话术" in node.instruction for node in response.draft_skill.nodes)


def test_fallback_card_uses_conservative_adaptive_steps() -> None:
    request = SkillDistillRequest(
        tenant_id="tenant_demo",
        title="预约服务",
        raw_content="获取用户姓名，确认预约人数，创建预约记录并反馈给用户",
    )

    card = SkillDistiller()._fallback_card(request)  # noqa: SLF001

    assert card.required_info == []
    assert all(
        not any(action.startswith("call_tool:") for action in node.allowed_actions)
        for node in card.nodes
    )
    assert any("目标而不是固定话术" in node.instruction for node in card.nodes)


def test_normalize_response_removes_unknown_actions_without_default_tool_suggestion() -> None:
    request = SkillDistillRequest(
        tenant_id="tenant_demo",
        title="商品比价",
        raw_content="用户提供两个商品名称，调用 product.compare 工具查询价格并反馈比价结果",
        available_tools=[],
    )
    raw = {
        "draft_skill": {
            "skill_id": "compare_products",
            "name": "商品比价",
            "required_info": ["product_name_1", "product_name_2"],
            "steps": [
                {
                    "step_id": "compare",
                    "name": "查询比价",
                    "instruction": "调用工具查询两个商品价格。",
                    "expected_user_info": ["product_name_1", "product_name_2"],
                    "allowed_actions": ["continue_flow", "call_tool:product.compare"],
                },
                {
                    "step_id": "reply_result",
                    "name": "反馈结果",
                    "instruction": "反馈比价结果。",
                    "expected_user_info": [],
                    "allowed_actions": ["answer_user"],
                },
            ],
            "response_rules": [],
        }
    }

    response = _normalize(raw, request)

    assert all(
        "call_tool:product.compare" not in node.allowed_actions
        for node in response.draft_skill.nodes
    )
    assert response.tool_suggestions == []
    assert any("未配置工具 product.compare" in warning for warning in response.warnings)


def test_normalize_response_resolves_tool_mentions_as_new_candidates() -> None:
    request = SkillDistillRequest(
        tenant_id="tenant_demo",
        title="商品比价",
        raw_content="用户提供两个商品名称，POST /api/mock/product/compare 使用两个商品名返回比价信息。",
        available_tools=[],
    )
    raw = {
        "draft_skill": {
            "skill_id": "compare_products",
            "name": "商品比价",
            "required_info": ["product_name_1", "product_name_2"],
            "steps": [
                {
                    "step_id": "compare",
                    "name": "查询比价",
                    "instruction": "调用工具查询两个商品价格。",
                    "expected_user_info": ["product_name_1", "product_name_2"],
                    "allowed_actions": ["continue_flow", "call_tool:product.compare"],
                },
                {
                    "step_id": "reply_result",
                    "name": "反馈结果",
                    "instruction": "反馈比价结果。",
                    "expected_user_info": [],
                    "allowed_actions": ["answer_user"],
                },
            ],
            "response_rules": [],
        },
        "tool_mentions": [
            {
                "name": "product.compare",
                "display_name": "商品比价查询",
                "description": "根据两个商品名称查询价格并返回对比信息。",
                "method": "POST",
                "url": "/api/mock/product/compare",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "product_name_1": {"type": "string"},
                        "product_name_2": {"type": "string"},
                    },
                    "required": ["product_name_1", "product_name_2"],
                },
                "output_schema": {
                    "type": "object",
                    "properties": {"success": {"type": "boolean"}, "data": {"type": "object"}},
                },
                "sample_arguments": {"product_name_1": "A1", "product_name_2": "A3"},
                "source_excerpt": "POST /api/mock/product/compare 使用两个商品名返回比价信息。",
                "reason": "原始流程需要商品比价能力，但当前没有对应工具。",
            }
        ],
    }

    response = _normalize(raw, request)

    assert "call_tool:product.compare" in response.draft_skill.nodes[0].allowed_actions
    assert [item.name for item in response.tool_suggestions] == ["product.compare"]
    assert response.tool_suggestions[0].resolution_status == "new_candidate"
    assert response.tool_suggestions[0].input_schema["required"] == ["product_name_1", "product_name_2"]
    assert response.tool_suggestions[0].sample_arguments == {"product_name_1": "A1", "product_name_2": "A3"}
    assert response.tool_suggestions[0].source_excerpt == "POST /api/mock/product/compare 使用两个商品名返回比价信息。"
    assert not any("未配置工具 product.compare" in warning and "已移出" in warning for warning in response.warnings)


def test_normalize_response_resolves_tool_mentions_as_existing_tools() -> None:
    request = SkillDistillRequest(
        tenant_id="tenant_demo",
        title="商品比价",
        raw_content="用户提供两个商品名称，POST /api/mock/product/compare 使用两个商品名返回比价信息。",
        available_tools=[
            {
                "id": "tool_1",
                "name": "product.compare",
                "display_name": "商品比价查询",
                "description": "根据两个商品名称查询价格并返回对比信息。",
                "method": "POST",
                "url": "/api/mock/product/compare",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "product_name_1": {"type": "string"},
                        "product_name_2": {"type": "string"},
                    },
                    "required": ["product_name_1", "product_name_2"],
                },
                "output_schema": {
                    "type": "object",
                    "properties": {"success": {"type": "boolean"}, "data": {"type": "object"}},
                },
            }
        ],
    )
    raw = {
        "draft_skill": {
            "skill_id": "compare_products",
            "name": "商品比价",
            "required_info": ["product_name_1", "product_name_2"],
            "steps": [
                {
                    "step_id": "compare",
                    "name": "查询比价",
                    "instruction": "调用工具查询两个商品价格。",
                    "expected_user_info": ["product_name_1", "product_name_2"],
                    "allowed_actions": ["continue_flow", "call_tool:product.compare"],
                },
                {
                    "step_id": "reply_result",
                    "name": "反馈结果",
                    "instruction": "反馈比价结果。",
                    "expected_user_info": [],
                    "allowed_actions": ["answer_user"],
                },
            ],
            "response_rules": [],
        },
        "tool_mentions": [
            {
                "name": "product.compare",
                "display_name": "商品比价查询",
                "description": "根据两个商品名称查询价格并返回对比信息。",
                "method": "POST",
                "url": "/api/mock/product/compare",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "product_name_1": {"type": "string"},
                        "product_name_2": {"type": "string"},
                    },
                    "required": ["product_name_1", "product_name_2"],
                },
                "output_schema": {
                    "type": "object",
                    "properties": {"success": {"type": "boolean"}, "data": {"type": "object"}},
                },
                "sample_arguments": {"product_name_1": "A1", "product_name_2": "A3"},
                "source_excerpt": "POST /api/mock/product/compare 使用两个商品名返回比价信息。",
                "reason": "原始流程明确提到商品比价接口。",
            }
        ],
    }

    response = _normalize(raw, request)

    assert response.tool_suggestions[0].resolution_status == "existing"
    assert response.tool_suggestions[0].matched_tool_name == "product.compare"
    assert response.tool_suggestions[0].matched_tool_id == "tool_1"
    assert response.tool_suggestions[0].url == "/api/mock/product/compare"
    assert "call_tool:product.compare" in response.draft_skill.nodes[0].allowed_actions


def test_normalize_response_drops_tool_suggestion_when_url_not_in_source() -> None:
    request = SkillDistillRequest(
        tenant_id="tenant_demo",
        title="会员权益补发",
        raw_content="核对会员权益差异，必要时补发权益并反馈处理结果。",
        available_tools=[],
    )
    raw = {
        "draft_skill": {
            "skill_id": "member_benefit",
            "name": "会员权益补发",
            "required_info": ["user_id", "order_id"],
            "steps": [
                {
                    "step_id": "issue_benefit",
                    "name": "补发权益",
                    "instruction": "补发会员权益。",
                    "expected_user_info": ["user_id", "order_id"],
                    "allowed_actions": ["call_tool:member.issue_benefit"],
                },
                {
                    "step_id": "reply_result",
                    "name": "反馈结果",
                    "instruction": "反馈处理结果。",
                    "expected_user_info": [],
                    "allowed_actions": ["answer_user"],
                },
            ],
            "response_rules": [],
        },
        "tool_mentions": [
            {
                "name": "member.issue_benefit",
                "display_name": "补发会员权益",
                "description": "补发会员权益。",
                "method": "POST",
                "url": "/api/member/issue-benefit",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string"},
                        "order_id": {"type": "string"},
                    },
                    "required": ["user_id", "order_id"],
                },
                "output_schema": {
                    "type": "object",
                    "properties": {"success": {"type": "boolean"}},
                },
                "sample_arguments": {"user_id": "user_demo", "order_id": "A12345"},
                "source_excerpt": "补发会员权益。",
                "reason": "文档描述了补发权益动作。",
            }
        ],
    }

    response = _normalize(raw, request)

    assert all(
        "call_tool:member.issue_benefit" not in node.allowed_actions
        for node in response.draft_skill.nodes
    )
    assert response.tool_suggestions == []
    assert any("未配置工具 member.issue_benefit" in warning for warning in response.warnings)
    assert any("当前不能新增" in warning for warning in response.warnings)


def test_normalize_response_does_not_suggest_tool_from_raw_text_only() -> None:
    request = SkillDistillRequest(
        tenant_id="tenant_demo",
        title="商品比价",
        raw_content="用户提供两个商品名称，使用 product.compare 工具查询价格并反馈比价结果",
        available_tools=[],
    )
    raw = {
        "draft_skill": {
            "skill_id": "compare_products",
            "name": "商品比价",
            "required_info": ["product_name_1", "product_name_2"],
            "steps": [
                {
                    "step_id": "collect",
                    "name": "收集商品",
                    "instruction": "收集两个商品名称。",
                    "expected_user_info": ["product_name_1", "product_name_2"],
                    "allowed_actions": ["ask_user"],
                },
                {
                    "step_id": "reply_result",
                    "name": "反馈结果",
                    "instruction": "反馈比价结果。",
                    "expected_user_info": [],
                    "allowed_actions": ["answer_user"],
                },
            ],
            "response_rules": [],
        }
    }

    response = _normalize(raw, request)

    assert response.tool_suggestions == []


def test_normalize_response_drops_incomplete_model_tool_suggestion() -> None:
    request = SkillDistillRequest(
        tenant_id="tenant_demo",
        title="商品比价",
        raw_content="用户提供两个商品名称，访问接口查询价格并反馈比价结果",
        available_tools=[],
    )
    raw = {
        "draft_skill": {
            "skill_id": "compare_products",
            "name": "商品比价",
            "required_info": ["product_name_1", "product_name_2"],
            "steps": [
                {
                    "step_id": "reply_result",
                    "name": "反馈结果",
                    "instruction": "反馈比价结果。",
                    "expected_user_info": [],
                    "allowed_actions": ["answer_user"],
                },
            ],
            "response_rules": [],
        },
        "tool_suggestions": [{"name": "product.compare"}],
    }

    response = _normalize(raw, request)

    assert response.tool_suggestions == []


def test_skill_card_serializes_response_rules_before_nodes() -> None:
    request = SkillDistillRequest(
        tenant_id="tenant_demo",
        title="资料审核",
        raw_content="收集资料编号，审核状态，反馈给用户",
    )

    card = SkillDistiller()._fallback_card(request)  # noqa: SLF001
    keys = list(card.model_dump().keys())

    assert "steps" not in keys
    assert keys.index("response_rules") < keys.index("nodes")
