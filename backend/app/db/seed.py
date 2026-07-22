from __future__ import annotations

import sys
from pathlib import Path

from sqlmodel import Session, select

from app import paths
from app.agents.branching import ensure_open_gallery_binding
from app.config import get_settings
from app.db.models import (
    AgentProfile,
    GeneralSkill,
    MCPServer,
    ModelConfig,
    PersonaConfig,
    Skill,
    Tenant,
    Tool,
    User,
    utc_now,
)
from app.security.encryption import encrypt_secret
from app.security.auth import hash_password
from app.db.staffdeck_seed import seed_staffdeck_admin_gallery


ADAPTIVE_FLOW_RULE = (
    "步骤是可自适应推进的目标，不是固定问答脚本；已由当前用户消息、历史信息或路由意图满足的内容"
    "不得重复追问，应直接推进到下一缺失信息、工具调用或最终回复。"
)


REFUND_SKILL = {
    "skill_id": "after_sales_refund",
    "name": "售后退款流程",
    "version": "1.0.0",
    "business_domain": "after_sales",
    "description": "处理用户退款、退货、取消订单等诉求。",
    "trigger_intents": ["退款", "退货", "取消订单", "不想要了"],
    "user_utterance_examples": ["我想退货", "这个不要了", "买错了能退吗", "给我退钱"],
    "goal": [
        "确认用户退款诉求",
        "收集订单号",
        "确认处理对象",
        "查询订单状态",
        "说明退款政策",
        "引导用户继续处理或转人工",
    ],
    "required_info": ["order_id", "refund_reason"],
    "slot_filling_policy": {
        "enabled": True,
        "multi_slot_per_turn": True,
        "extract_scope": "all_skill_expected_user_info",
        "skip_satisfied_steps": True,
        "description": "每轮同时抽取用户已表达的退款类型、订单号、退款原因和确认意愿等信息，已满足的信息不再追问。",
        "target_info": ["refund_type", "order_id", "order_confirmed", "refund_reason"],
    },
    "nodes": [
        {
            "node_id": "identify_refund_intent",
            "name": "确认退款诉求",
            "instruction": "将本步骤作为目标而不是固定话术；仅当用户诉求不明确时确认用户是否要退款、退货或取消订单；如果用户已明确说退货/退款/取消订单，写入 refund_type 并直接进入下一缺失信息收集，不要反问类型。",
            "expected_user_info": ["refund_type"],
            "allowed_actions": ["ask_clarification", "continue_flow"],
        },
        {
            "node_id": "collect_order_info",
            "name": "收集订单信息",
            "instruction": "将本步骤作为目标而不是固定话术；如果用户未提供订单号，直接询问订单号；如果用户明确提供订单号，写入 order_id 并进入确认步骤；如果 order_id 是根据 recent_messages、上一笔订单或上下文推断出来的，必须进入确认步骤，不得直接调用工具。不要再询问用户是退货还是退款。",
            "expected_user_info": ["order_id"],
            "allowed_actions": ["ask_user", "continue_flow"],
        },
        {
            "node_id": "confirm_refund_order",
            "name": "确认售后订单",
            "instruction": "在查询或处理退款/退货/取消订单前，必须向用户确认本次要处理的订单号和诉求类型。只有用户明确确认后，才能写入 order_confirmed=true 并继续；如果用户说不是、另一个、换一个，应清空或更新 order_id 并回到订单信息收集。",
            "expected_user_info": ["order_confirmed"],
            "allowed_actions": ["ask_user", "continue_flow"],
        },
        {
            "node_id": "check_refund_eligibility",
            "name": "查询退款资格",
            "instruction": "将本步骤作为目标而不是固定话术；仅当 order_id 已存在且 order_confirmed=true 时调用 order.query；根据订单查询结果说明是否可能支持退款/退货，不要承诺一定成功；如还缺原因则继续收集，已满足时给出明确下一步。",
            "expected_user_info": [],
            "allowed_actions": [
                "continue_flow",
                "call_tool:order.query",
                "answer_user",
                "handoff_human",
            ],
        },
        {
            "node_id": "collect_refund_reason",
            "name": "收集退款原因",
            "instruction": "将本步骤作为目标而不是固定话术；如果用户已说明退款原因，写入 refund_reason 并继续推进；否则只追问退款原因，不重复追问退款类型或订单号。",
            "expected_user_info": ["refund_reason"],
            "allowed_actions": ["ask_user", "continue_flow"],
        },
    ],
    "interruption_policy": {
        "related_question": "可以临时回答，回答后回到当前退款流程。",
        "unrelated_business": "可以切换到新技能，并保存当前流程进度。",
        "chitchat": "简短回应后，引导用户继续退款流程。",
        "user_wants_human": "直接转人工。",
    },
    "response_rules": [
        "不要承诺一定能退款。",
        "未查询订单前，不要判断是否符合退款条件。",
        "退款、退货或取消订单前必须先向用户确认订单号和诉求类型。",
        "如果用户要求人工，应转人工。",
        ADAPTIVE_FLOW_RULE,
    ],
}

EXCHANGE_SKILL = {
    "skill_id": "after_sales_exchange",
    "name": "售后换货流程",
    "version": "1.0.0",
    "business_domain": "after_sales",
    "description": "处理用户换货、更换商品、尺码颜色不合适等诉求。",
    "trigger_intents": ["换货", "更换商品", "换尺码", "换颜色"],
    "user_utterance_examples": ["我想换货", "能不能换个颜色", "尺码不合适想换一下"],
    "goal": ["确认换货诉求", "收集订单号", "确认换货原因", "引导用户继续处理或转人工"],
    "required_info": ["order_id", "exchange_reason"],
    "slot_filling_policy": {
        "enabled": True,
        "multi_slot_per_turn": True,
        "extract_scope": "all_skill_expected_user_info",
        "skip_satisfied_steps": True,
        "description": "每轮同时抽取用户已表达的换货类型、订单号、换货原因等信息，已满足的信息不再追问。",
        "target_info": ["exchange_type", "order_id", "exchange_reason"],
    },
    "nodes": [
        {
            "node_id": "identify_exchange_intent",
            "name": "确认换货诉求",
            "instruction": "将本步骤作为目标而不是固定话术；如果用户已表达换货商品或换货类型，写入 exchange_type 并继续推进；仅在诉求不明确时追问。",
            "expected_user_info": ["exchange_type"],
            "allowed_actions": ["ask_clarification", "continue_flow"],
        },
        {
            "node_id": "collect_exchange_order_info",
            "name": "收集订单信息",
            "instruction": "将本步骤作为目标而不是固定话术；如果用户已提供订单号，写入 order_id 并调用 order.query；否则询问订单号，并只追问真正缺失的换货信息。",
            "expected_user_info": ["order_id"],
            "allowed_actions": ["ask_user", "call_tool:order.query"],
        },
    ],
    "interruption_policy": {
        "related_question": "可以临时回答，回答后回到当前换货流程。",
        "unrelated_business": "可以切换到新技能，并保存当前流程进度。",
        "chitchat": "简短回应后，引导用户继续换货流程。",
        "user_wants_human": "直接转人工。",
    },
    "response_rules": ["不要承诺一定能换货。", "如政策不确定，应转人工确认。", ADAPTIVE_FLOW_RULE],
}

PURCHASE_SKILL = {
    "skill_id": "skill_purchase_001",
    "name": "购买商品流程",
    "version": "1.0.0",
    "business_domain": "commerce",
    "description": "引导用户完成商品购买流程，包括收集用户信息、确认商品、生成订单并反馈结果。",
    "trigger_intents": ["购买商品", "下单", "买东西", "购买", "place_order"],
    "user_utterance_examples": ["我想买这个商品", "帮我下单", "我要购买 A1", "我要买一个a1"],
    "goal": [
        "获取用户身份信息",
        "确认购买的商品及数量",
        "确认下单意愿",
        "生成有效订单",
        "向用户反馈订单号及状态",
    ],
    "required_info": ["user_name", "product_id", "quantity"],
    "slot_filling_policy": {
        "enabled": True,
        "multi_slot_per_turn": True,
        "extract_scope": "all_skill_expected_user_info",
        "skip_satisfied_steps": True,
        "description": "每轮同时抽取用户已表达的姓名、商品 ID、购买数量和下单确认等信息；数量需理解口语数字和量词表达，已满足的信息不再追问。",
        "target_info": ["user_name", "product_id", "quantity", "purchase_confirmed"],
    },
    "nodes": [
        {
            "node_id": "collect_user_name",
            "name": "收集用户信息与商品详情",
            "instruction": (
                "将本步骤作为目标而不是固定话术；同时收集用户姓名、商品 ID 和数量。"
                "用户一句话提供多个信息时必须一次性写入 slot_updates；"
                "数值字段需要理解口语数字和量词表达，例如“一个/一件/一台”表示 1，“两个/两件”表示 2，“三份/3个”表示 3。"
                "已提供的信息不再追问，只追问真正缺失的信息；全部满足后进入下单确认，不要直接创建订单。"
            ),
            "expected_user_info": ["user_name", "product_id", "quantity"],
            "allowed_actions": ["ask_user", "continue_flow"],
        },
        {
            "node_id": "confirm_purchase",
            "name": "确认下单信息",
            "instruction": "创建订单前必须向用户确认姓名、商品 ID 和数量。只有用户明确确认后，才能写入 purchase_confirmed=true 并继续；如果用户修改商品、数量或姓名，应更新对应 slot 并重新确认。",
            "expected_user_info": ["purchase_confirmed"],
            "allowed_actions": ["ask_user", "continue_flow"],
        },
        {
            "node_id": "confirm_product",
            "name": "执行购买/创建订单",
            "instruction": (
                "将本步骤作为目标而不是固定话术；仅当 user_name、product_id、quantity 已满足且 purchase_confirmed=true 时，"
                "直接调用 product.purchase 或 order.add 创建订单，不要重复确认商品或数量。"
                "如果工具需要 user_id 且只有 user_name，可将 user_name 作为 user_id。"
            ),
            "expected_user_info": ["product_id", "quantity", "purchase_confirmed"],
            "allowed_actions": [
                "continue_flow",
                "call_tool:product.purchase",
                "call_tool:order.add",
            ],
        },
        {
            "node_id": "create_order",
            "name": "反馈订单结果",
            "instruction": "将工具返回的订单号、商品信息、数量、金额和状态告知用户，确认购买结果；不要只说请稍候。",
            "expected_user_info": [],
            "allowed_actions": ["answer_user"],
        },
    ],
    "interruption_policy": {
        "related_question": "可以临时回答，回答后回到当前购买流程。",
        "unrelated_business": "可以切换到新技能，并保存当前流程进度。",
        "chitchat": "简短回应后，引导用户继续购买流程。",
        "user_wants_human": "直接转人工。",
    },
    "response_rules": [
        "保持语气友好、专业。",
        "明确告知用户订单号。",
        "创建订单前必须先向用户确认姓名、商品 ID 和数量。",
        "若商品不存在或库存不足，需明确告知用户并建议其他操作。",
        ADAPTIVE_FLOW_RULE,
    ],
}

PRICE_COMPARE_SKILL = {
    "skill_id": "skill_price_compare_001",
    "name": "商品比价服务",
    "version": "1.0.0",
    "business_domain": "commerce",
    "description": "根据用户提供的两个商品名称，查询价格、品牌和规格后给出比价结果。",
    "trigger_intents": ["商品比价", "价格对比", "比下价格", "比较价格", "哪个更便宜"],
    "user_utterance_examples": [
        "帮我比一下 A1 和 A3 的价格",
        "买之前想看看 A1 和 iPhone 15 哪个更划算",
        "A1 跟 A3 价格差多少",
    ],
    "goal": ["收集两个待比价商品", "分别查询商品价格", "基于工具结果给出比价结论"],
    "required_info": ["product_name_1", "product_name_2"],
    "slot_filling_policy": {
        "enabled": True,
        "multi_slot_per_turn": True,
        "extract_scope": "all_skill_expected_user_info",
        "skip_satisfied_steps": True,
        "description": "每轮同时抽取用户提到的两个待比较商品名称；如果只给出一个商品，应只追问另一个。",
        "target_info": ["product_name_1", "product_name_2"],
    },
    "nodes": [
        {
            "node_id": "collect_products",
            "name": "收集待比价商品",
            "instruction": (
                "将本步骤作为目标而不是固定话术；从当前消息、历史对话和 slots 中识别两个待比价商品。"
                "用户一次给出两个商品时，必须同时写入 product_name_1 和 product_name_2 并继续；"
                "只缺一个商品时只追问缺失的那个，不要重复确认已给出的商品。"
            ),
            "expected_user_info": ["product_name_1", "product_name_2"],
            "allowed_actions": ["ask_user", "continue_flow"],
        },
        {
            "node_id": "query_prices",
            "name": "查询商品价格",
            "instruction": (
                "当 product_name_1 和 product_name_2 都已获得时，依次调用 product.price_query 查询两个商品。"
                "不要编造价格；如果只查到一个商品，应继续调用工具查询另一个商品；"
                "两个工具结果都齐全后进入结果回复。"
            ),
            "expected_user_info": [],
            "allowed_actions": ["call_tool:product.price_query", "continue_flow"],
        },
        {
            "node_id": "reply_compare_result",
            "name": "反馈比价结果",
            "instruction": (
                "基于累计工具结果对比两个商品的价格、品牌和规格，说明哪个更便宜、差价多少；"
                "如果某个商品未找到或工具失败，应明确说明无法完成该商品的比价，并给出下一步建议。"
            ),
            "expected_user_info": [],
            "allowed_actions": ["answer_user"],
        },
    ],
    "interruption_policy": {
        "related_question": "可以临时回答，回答后回到当前比价流程。",
        "unrelated_business": "可以切换到新技能，并保存当前流程进度。",
        "chitchat": "简短回应后，引导用户继续比价流程。",
        "user_wants_human": "直接转人工。",
    },
    "response_rules": [
        "不要在没有工具结果时编造价格。",
        "若工具未查到商品，应明确说明并请用户更换商品名或转人工。",
        "比价结论必须引用工具返回的价格、品牌或规格信息。",
        ADAPTIVE_FLOW_RULE,
    ],
}

GRAPH_VISUAL_DEMO_SKILL = {
    "skill_id": "skill_graph_visual_demo",
    "name": "图结构可视化验证流程",
    "version": "1.0.0",
    "business_domain": "demo",
    "description": "用于验证 graph-only 技能流程图的分支、可选节点、工具节点、知识节点和终止节点展示效果。",
    "trigger_intents": ["图结构验证", "流程图验证", "graph demo", "验证分支流程"],
    "user_utterance_examples": [
        "帮我跑一下图结构验证",
        "我要验证一个包含分支和工具的流程",
        "这个流程需要先查价格再确认",
    ],
    "goal": [
        "识别用户要验证的处理路径",
        "按条件进入工具或知识分支",
        "必要时确认",
        "给出最终结果或转人工",
    ],
    "required_info": ["request_type"],
    "slot_filling_policy": {
        "enabled": True,
        "multi_slot_per_turn": True,
        "extract_scope": "all_skill_expected_user_info",
        "skip_satisfied_steps": True,
        "target_info": ["request_type", "product_name", "confirmation"],
    },
    "nodes": [
        {
            "node_id": "intake_request",
            "type": "collect_info",
            "name": "识别验证请求",
            "instruction": "识别用户想验证的是工具路径、知识路径、直接确认路径还是人工路径；若用户已说明目标，写入 request_type 并推进。",
            "expected_user_info": ["request_type"],
            "allowed_actions": ["ask_user", "continue_flow"],
        },
        {
            "node_id": "classify_path",
            "type": "decision",
            "name": "选择处理分支",
            "instruction": "根据 request_type 选择后续路径：需要外部数据时进入工具节点；需要政策依据时进入知识节点；已满足条件时进入确认节点；无法判断时转人工。",
            "expected_user_info": [],
            "allowed_actions": ["continue_flow", "handoff_human"],
        },
        {
            "node_id": "query_product_price",
            "type": "tool_call",
            "name": "查询商品价格",
            "instruction": "当用户提供商品名或要求验证工具分支时，调用 product.price_query 查询商品价格、品牌和规格；工具失败时让模型基于结果决定重试、换路径或追问。",
            "expected_user_info": ["product_name"],
            "allowed_actions": ["ask_user", "call_tool:product.price_query", "continue_flow"],
            "retry_policy": {"max_attempts": 2, "on_failure": "reflect"},
        },
        {
            "node_id": "read_policy_knowledge",
            "type": "knowledge_query",
            "name": "读取处理依据",
            "instruction": "当用户需要解释规则或依据时，检索当前智能体可见知识库中的相关桶和片段，并把知识结果交给模型继续判断。",
            "expected_user_info": [],
            "allowed_actions": ["knowledge_query", "continue_flow"],
            "knowledge_scope": {"bucket_hint": "demo_policy"},
        },
        {
            "node_id": "confirm_action",
            "type": "decision",
            "name": "可选确认",
            "instruction": "如动作会产生业务影响，先向用户确认；若用户已经明确确认，可跳过追问并继续回复。",
            "optional": True,
            "expected_user_info": ["confirmation"],
            "allowed_actions": ["ask_user", "continue_flow"],
        },
        {
            "node_id": "reply_result",
            "type": "response",
            "name": "反馈验证结果",
            "instruction": "汇总已选择的分支、工具结果或知识依据，用简洁语言反馈本次 graph 流程验证结果。",
            "expected_user_info": [],
            "allowed_actions": ["answer_user"],
        },
        {
            "node_id": "handoff_manual",
            "type": "handoff",
            "name": "转人工处理",
            "instruction": "当用户明确要求人工或模型判断无法可靠完成时，说明需要人工继续处理。",
            "expected_user_info": [],
            "allowed_actions": ["handoff_human"],
        },
    ],
    "edges": [
        {
            "source_node_id": "intake_request",
            "next_node_id": "classify_path",
            "condition": "request_type 已识别",
            "priority": 0,
            "label": "进入分支判断",
        },
        {
            "source_node_id": "classify_path",
            "next_node_id": "query_product_price",
            "condition": "需要外部商品数据",
            "priority": 0,
            "label": "工具路径",
        },
        {
            "source_node_id": "classify_path",
            "next_node_id": "read_policy_knowledge",
            "condition": "需要知识依据",
            "priority": 1,
            "label": "知识路径",
        },
        {
            "source_node_id": "classify_path",
            "next_node_id": "confirm_action",
            "condition": "信息充分但需要确认",
            "priority": 2,
            "label": "确认路径",
        },
        {
            "source_node_id": "classify_path",
            "next_node_id": "handoff_manual",
            "condition": "用户要求人工或无法判断",
            "priority": 3,
            "label": "人工路径",
        },
        {
            "source_node_id": "query_product_price",
            "next_node_id": "confirm_action",
            "condition": "工具结果可用",
            "priority": 0,
            "label": "核验后确认",
        },
        {
            "source_node_id": "query_product_price",
            "next_node_id": "handoff_manual",
            "condition": "工具失败且反思后仍无法处理",
            "priority": 1,
            "label": "工具失败",
        },
        {
            "source_node_id": "read_policy_knowledge",
            "next_node_id": "reply_result",
            "condition": "知识依据足够",
            "priority": 0,
            "label": "依据充分",
        },
        {
            "source_node_id": "confirm_action",
            "next_node_id": "reply_result",
            "condition": "用户确认或可跳过确认",
            "priority": 0,
            "label": "完成确认",
        },
        {
            "source_node_id": "confirm_action",
            "next_node_id": "handoff_manual",
            "condition": "用户拒绝或需要人工",
            "priority": 1,
            "label": "确认失败",
        },
    ],
    "start_node_id": "intake_request",
    "terminal_node_ids": ["reply_result", "handoff_manual"],
    "interruption_policy": {
        "related_question": "可以回答后继续当前验证流程。",
        "unrelated_business": "可保存当前验证流程并切换任务。",
        "chitchat": "简短回应后继续引导用户完成验证。",
        "user_wants_human": "直接转人工。",
    },
    "response_rules": [
        "不要编造工具结果。",
        "涉及知识依据时必须基于检索结果回复。",
        ADAPTIVE_FLOW_RULE,
    ],
}

ORDER_QUERY_TOOL = {
    "name": "order.query",
    "display_name": "订单查询",
    "description": "根据订单号查询订单状态、签收天数和是否可能支持退款。",
    "bucket": "订单工具",
    "method": "POST",
    "url": "/api/mock/order/query",
    "headers_json": {},
    "auth_json": {},
    "input_schema": {
        "type": "object",
        "properties": {"order_id": {"type": "string", "description": "订单号"}},
        "required": ["order_id"],
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "order_id": {"type": "string"},
            "found": {"type": "boolean"},
            "status": {"type": "string"},
            "signed_days": {"type": "integer"},
            "refundable": {"type": "boolean"},
            "miss_reason": {"type": "string"},
        },
    },
    "allowed_skills_json": ["after_sales_refund", "after_sales_exchange"],
    "enabled": True,
}

ORDER_ARCHIVE_QUERY_TOOL = {
    "name": "order.archive_query",
    "display_name": "历史订单查询",
    "description": "备用订单查询工具；当 order.query 主订单中心未命中、found=false、miss_reason 或历史订单场景时，用同一 order_id 查询归档订单。",
    "bucket": "订单工具",
    "method": "POST",
    "url": "/api/mock/order/archive-query",
    "headers_json": {},
    "auth_json": {},
    "input_schema": {
        "type": "object",
        "properties": {"order_id": {"type": "string", "description": "订单号"}},
        "required": ["order_id"],
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "order_id": {"type": "string"},
            "found": {"type": "boolean"},
            "source": {"type": "string"},
            "status": {"type": "string"},
            "signed_days": {"type": "integer"},
            "refundable": {"type": "boolean"},
            "recommendation": {"type": "string"},
        },
    },
    "allowed_skills_json": ["after_sales_refund", "after_sales_exchange"],
    "enabled": True,
}

PRODUCT_PURCHASE_TOOL = {
    "name": "product.purchase",
    "display_name": "购买商品",
    "description": "模拟用户购买商品，返回支付后的订单与购买记录。",
    "bucket": "商品工具",
    "method": "POST",
    "url": "/api/mock/product/purchase",
    "headers_json": {},
    "auth_json": {},
    "input_schema": {
        "type": "object",
        "properties": {
            "user_id": {"type": "string", "description": "用户 ID"},
            "product_id": {"type": "string", "description": "商品 ID，如 SKU-001"},
            "sku_id": {"type": "string", "description": "可选 SKU ID"},
            "quantity": {"type": "integer", "minimum": 1, "maximum": 99, "description": "购买数量"},
            "payment_method": {"type": "string", "description": "支付方式"},
        },
        "required": ["product_id"],
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "found": {"type": "boolean"},
            "order_id": {"type": "string"},
            "purchase_id": {"type": "string"},
            "product_id": {"type": "string"},
            "display_name": {"type": "string"},
            "quantity": {"type": "integer"},
            "unit_price": {"type": "number"},
            "payment_status": {"type": "string"},
            "order_status": {"type": "string"},
            "total_amount": {"type": "number"},
            "currency": {"type": "string"},
        },
    },
    "allowed_skills_json": [],
    "enabled": True,
}

ORDER_ADD_TOOL = {
    "name": "order.add",
    "display_name": "订单添加",
    "description": "模拟新增一笔订单，返回订单号、商品、金额和订单状态。",
    "bucket": "订单工具",
    "method": "POST",
    "url": "/api/mock/order/add",
    "headers_json": {},
    "auth_json": {},
    "input_schema": {
        "type": "object",
        "properties": {
            "user_id": {"type": "string", "description": "用户 ID"},
            "order_id": {"type": "string", "description": "可选自定义订单号"},
            "product_id": {"type": "string", "description": "商品 ID，如 SKU-001"},
            "sku_id": {"type": "string", "description": "可选 SKU ID"},
            "quantity": {"type": "integer", "minimum": 1, "maximum": 99, "description": "商品数量"},
            "status": {"type": "string", "description": "订单初始状态"},
        },
        "required": ["product_id"],
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "found": {"type": "boolean"},
            "order_id": {"type": "string"},
            "user_id": {"type": "string"},
            "product_id": {"type": "string"},
            "display_name": {"type": "string"},
            "quantity": {"type": "integer"},
            "unit_price": {"type": "number"},
            "status": {"type": "string"},
            "total_amount": {"type": "number"},
            "currency": {"type": "string"},
        },
    },
    "allowed_skills_json": [],
    "enabled": True,
}

PRODUCT_PRICE_QUERY_TOOL = {
    "name": "product.price_query",
    "display_name": "商品价格查询",
    "description": "根据商品名称查询商品价格、品牌、规格和更新时间，用于商品比价。",
    "bucket": "商品工具",
    "method": "POST",
    "url": "/api/mock/product/price-query",
    "headers_json": {},
    "auth_json": {},
    "input_schema": {
        "type": "object",
        "properties": {
            "product_name": {
                "type": "string",
                "description": "商品名称或商品别名，如 A1、A3、iPhone 15",
            }
        },
        "required": ["product_name"],
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "product_name": {"type": "string"},
            "found": {"type": "boolean"},
            "source": {"type": "string"},
            "product_id": {"type": "string"},
            "display_name": {"type": "string"},
            "brand": {"type": "string"},
            "price": {"type": "number"},
            "currency": {"type": "string"},
            "spec": {"type": "string"},
            "updated_at": {"type": "string"},
        },
    },
    "allowed_skills_json": ["skill_price_compare_001", "skill_graph_visual_demo"],
    "enabled": True,
}

MOCK_MCP_STDIO_SERVER = paths.resource_dir() / "mock_servers" / "mcp_stdio_server.py"


def _stdio_mcp_python() -> str:
    # 打包态 sys.executable 指向 ultrarag 引导器，需用附带 Python
    if paths.is_frozen():
        from app.general_skills.runtime_env import _bundled_python

        bundled = _bundled_python()
        if bundled.exists():
            return str(bundled)
    return sys.executable


# --------------------------------------------------------------------------- #
# MCP Servers（工具集）与其发现出的子工具
# --------------------------------------------------------------------------- #

MCP_BUILTIN_DEMO_SERVER = {
    "name": "builtin_demo",
    "display_name": "内置 Demo MCP",
    "description": "内置 MCP demo server，用于验证 MCP 工具集的连接、发现与调用链路。",
    "bucket": "MCP 工具",
    "transport": "builtin",
    "url": None,
    "headers_json": {},
    "command": None,
    "args_json": [],
    "env_json": {},
    "cwd": None,
    "enabled": True,
}

MCP_STDIO_DEMO_SERVER = {
    "name": "stdio_demo",
    "display_name": "Stdio Demo MCP",
    "description": "真实 stdio MCP mock server，用于验证 MCP client transport、初始化和 tools/list、tools/call 链路。",
    "bucket": "MCP 工具",
    "transport": "stdio",
    "url": None,
    "headers_json": {},
    "command": None,  # 由 _seed_mcp_servers 运行时惰性注入（见下）
    "args_json": [str(MOCK_MCP_STDIO_SERVER)],
    "env_json": {},
    "cwd": None,
    "enabled": True,
}

MCP_SERVERS = (
    MCP_BUILTIN_DEMO_SERVER,
    MCP_STDIO_DEMO_SERVER,
)

# 每个 MCP server 预先落地的子工具（模拟已执行过一次「发现/同步」）。
# config_json 只放 leaf tool 名，连接配置由 mcp_server_id 关联的 server 提供。
MCP_SERVER_TOOLS = {
    "builtin_demo": [
        {
            "leaf": "echo",
            "display_name": "MCP Demo Echo",
            "description": "内置 MCP demo echo 工具，回显文本并返回长度。",
            "input_schema": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "要回显的文本",
                        "example": "hello mcp",
                    }
                },
                "required": ["text"],
            },
            "output_schema": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "length": {"type": "integer"}},
            },
            "allowed_skills_json": [],
        },
    ],
    "stdio_demo": [
        {
            "leaf": "product_lookup",
            "display_name": "MCP Stdio 商品查询",
            "description": "stdio MCP mock server 的商品查询工具。",
            "input_schema": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "string", "description": "商品 ID，例如 A1 或 A3"}
                },
                "required": ["product_id"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "found": {"type": "boolean"},
                    "product_id": {"type": "string"},
                    "display_name": {"type": "string"},
                    "price": {"type": "number"},
                    "currency": {"type": "string"},
                },
            },
            "allowed_skills_json": ["skill_price_compare_001", "skill_graph_visual_demo"],
        },
    ],
}

DEMO_TOOLS = (
    ORDER_QUERY_TOOL,
    ORDER_ARCHIVE_QUERY_TOOL,
    PRODUCT_PURCHASE_TOOL,
    ORDER_ADD_TOOL,
    PRODUCT_PRICE_QUERY_TOOL,
)
DEFAULT_PERSONA_PROMPT = (
    "你是面壁智能的智能客服，语气专业、清晰、友好。"
    "你需要先理解用户诉求，再基于已配置的技能和工具帮助用户完成业务办理。"
    "不要暴露内部路由、技能 ID、步骤 ID 或工具实现细节。"
)


def _seed_mcp_servers(session: Session) -> None:
    """落地 demo MCP server（工具集）及其已发现的子工具。"""
    for server_config in MCP_SERVERS:
        server_config = dict(server_config)  # 避免修改模块级常量
        if server_config.get("name") == "stdio_demo":
            server_config["command"] = _stdio_mcp_python()
        server = session.exec(
            select(MCPServer).where(
                MCPServer.tenant_id == "tenant_demo", MCPServer.name == server_config["name"]
            )
        ).first()
        if not server:
            server = MCPServer(tenant_id="tenant_demo", **server_config)
            session.add(server)
            session.flush()
        else:
            for key, value in server_config.items():
                setattr(server, key, value)
            server.updated_at = utc_now()
            session.add(server)

        for tool_def in MCP_SERVER_TOOLS.get(server_config["name"], []):
            leaf = tool_def["leaf"]
            scoped_name = f"{server.name}.{leaf}"
            tool = session.exec(
                select(Tool).where(Tool.tenant_id == "tenant_demo", Tool.name == scoped_name)
            ).first()
            payload = {
                "display_name": tool_def.get("display_name") or leaf,
                "description": tool_def.get("description") or "",
                "bucket": server.bucket or "MCP 工具",
                "tool_type": "mcp",
                "method": "POST",
                "url": f"mcp://{server.name}/{leaf}",
                "headers_json": {},
                "auth_json": {},
                "config_json": {"tool": leaf},
                "input_schema": tool_def.get("input_schema") or {},
                "output_schema": tool_def.get("output_schema") or {},
                "allowed_skills_json": tool_def.get("allowed_skills_json") or [],
                "mcp_server_id": server.id,
                "enabled": True,
            }
            if not tool:
                session.add(Tool(tenant_id="tenant_demo", name=scoped_name, **payload))
            else:
                for key, value in payload.items():
                    setattr(tool, key, value)
                tool.updated_at = utc_now()
                session.add(tool)
        server.last_synced_at = utc_now()
        session.add(server)


def seed_demo_data(session: Session) -> None:
    settings = get_settings()
    if not session.get(Tenant, "tenant_demo"):
        session.add(Tenant(id="tenant_demo", name="Demo Enterprise"))

    if not session.get(PersonaConfig, "tenant_demo"):
        session.add(PersonaConfig(tenant_id="tenant_demo", system_prompt=DEFAULT_PERSONA_PROMPT))

    demo_user = session.exec(
        select(User).where(User.tenant_id == "tenant_demo", User.username == "user_demo")
    ).first()
    if not demo_user:
        session.add(
            User(
                id="user_demo",
                tenant_id="tenant_demo",
                username="user_demo",
                display_name="Demo User",
                password_hash=hash_password("demo"),
            )
        )

    # 桌面/单机版默认管理员账号（admin / admin）。权限只读取数据库 role 字段。
    admin_user = session.exec(
        select(User).where(User.tenant_id == "tenant_demo", User.username == "admin")
    ).first()
    if not admin_user:
        session.add(
            User(
                id="admin",
                tenant_id="tenant_demo",
                username="admin",
                display_name="Administrator",
                role="admin",
                password_hash=hash_password("admin"),
            )
        )
    elif admin_user.role != "admin":
        admin_user.role = "admin"
        admin_user.updated_at = utc_now()
        session.add(admin_user)

    _ensure_seed_agents(session)

    for raw_content in (
        REFUND_SKILL,
        EXCHANGE_SKILL,
        PURCHASE_SKILL,
        PRICE_COMPARE_SKILL,
        GRAPH_VISUAL_DEMO_SKILL,
    ):
        content = _skill_content_graph(raw_content)
        existing = session.exec(
            select(Skill).where(
                Skill.tenant_id == "tenant_demo", Skill.skill_id == content["skill_id"]
            )
        ).first()
        if not existing:
            session.add(
                Skill(
                    tenant_id="tenant_demo",
                    skill_id=content["skill_id"],
                    version=content["version"],
                    name=content["name"],
                    business_domain=content["business_domain"],
                    description=content["description"],
                    content_json=content,
                    status="published",
                )
            )
        else:
            _sync_demo_skill_if_stale(existing, content)

    for tool_config in DEMO_TOOLS:
        tool_config = _tool_config_with_base_url(tool_config, settings.normalized_tool_base_url)
        tool = session.exec(
            select(Tool).where(Tool.tenant_id == "tenant_demo", Tool.name == tool_config["name"])
        ).first()
        if not tool:
            session.add(Tool(tenant_id="tenant_demo", **tool_config))
        else:
            tool.bucket = tool_config.get("bucket") or tool.bucket or "未分桶"
            tool.display_name = tool_config.get("display_name") or tool.display_name
            tool.description = tool_config.get("description") or tool.description
            tool.method = tool_config.get("method") or tool.method
            tool.url = tool_config.get("url") or tool.url
            tool.tool_type = (
                tool_config.get("tool_type") or getattr(tool, "tool_type", None) or "http"
            )
            tool.headers_json = tool_config.get("headers_json") or tool.headers_json
            tool.auth_json = tool_config.get("auth_json") or tool.auth_json
            tool.config_json = tool_config.get("config_json") or tool.config_json
            tool.input_schema = tool_config.get("input_schema") or tool.input_schema
            tool.output_schema = tool_config.get("output_schema") or tool.output_schema
            configured_skills = [
                str(skill_id)
                for skill_id in (tool_config.get("allowed_skills_json") or [])
                if str(skill_id).strip()
            ]
            existing_skills = [
                str(skill_id)
                for skill_id in (tool.allowed_skills_json or [])
                if str(skill_id).strip()
            ]
            tool.allowed_skills_json = list(
                dict.fromkeys([*configured_skills, *existing_skills])
            )
            tool.enabled = bool(tool_config.get("enabled", tool.enabled))
            tool.updated_at = utc_now()
            session.add(tool)

    _seed_mcp_servers(session)
    _seed_weather_general_skill(session)
    session.flush()
    _publish_seeded_system_resources(session)
    seed_staffdeck_admin_gallery(session)

    default_model = session.exec(
        select(ModelConfig).where(
            ModelConfig.tenant_id == "tenant_demo",
            ModelConfig.is_default == True,  # noqa: E712
        )
    ).first()
    if not default_model and settings.demo_model_api_key:
        session.add(
            ModelConfig(
                tenant_id="tenant_demo",
                name="Demo Qwen Compatible",
                provider="openai_compatible",
                api_protocol="openai_chat_completions",
                base_url=settings.demo_model_base_url,
                api_key_encrypted=encrypt_secret(settings.demo_model_api_key),
                model=settings.demo_model_name,
                temperature=0.2,
                max_output_tokens=8192,
                trust_status="legacy_trusted",
                is_default=True,
                enabled=True,
            )
        )

    session.commit()


def _publish_seeded_system_resources(session: Session) -> None:
    tenant_id = "tenant_demo"
    creator_metadata = _system_seed_metadata()

    overall = session.get(AgentProfile, f"agent_{tenant_id}_overall")
    if overall:
        overall.metadata_json = _system_seed_metadata(overall.metadata_json or {})
        session.add(overall)

    _archive_seed_default_agent(session, tenant_id)

    seeded_skill_ids = {
        str(content["skill_id"])
        for content in (
            REFUND_SKILL,
            EXCHANGE_SKILL,
            PURCHASE_SKILL,
            PRICE_COMPARE_SKILL,
            GRAPH_VISUAL_DEMO_SKILL,
        )
    }
    for skill in session.exec(
        select(Skill).where(Skill.tenant_id == tenant_id, Skill.skill_id.in_(seeded_skill_ids))
    ).all():
        ensure_open_gallery_binding(
            session,
            tenant_id,
            "skill",
            skill.id,
            "active" if skill.status == "published" else "inactive",
            metadata_json=creator_metadata,
        )

    seeded_tool_names = {str(config["name"]) for config in DEMO_TOOLS}
    for tool in session.exec(
        select(Tool).where(Tool.tenant_id == tenant_id, Tool.name.in_(seeded_tool_names))
    ).all():
        ensure_open_gallery_binding(
            session,
            tenant_id,
            "tool",
            tool.id,
            "active" if tool.enabled else "inactive",
            metadata_json=creator_metadata,
        )

    weather = session.exec(
        select(GeneralSkill).where(
            GeneralSkill.tenant_id == tenant_id, GeneralSkill.slug == "weather-zh"
        )
    ).first()
    if weather:
        weather.metadata_json = _system_seed_metadata(weather.metadata_json or {})
        session.add(weather)
        ensure_open_gallery_binding(
            session,
            tenant_id,
            "general_skill",
            weather.id,
            "active" if weather.status == "published" else "inactive",
            metadata_json=creator_metadata,
        )


def _ensure_seed_agents(session: Session) -> None:
    tenant_id = "tenant_demo"
    for agent_id, name, description, is_overall in (
        (f"agent_{tenant_id}_overall", "整体智能体", "全局资源池", True),
    ):
        existing = session.get(AgentProfile, agent_id)
        if existing:
            continue
        session.add(
            AgentProfile(
                id=agent_id,
                tenant_id=tenant_id,
                name=name,
                description=description,
                is_overall=is_overall,
                status="active",
            )
        )


def _archive_seed_default_agent(session: Session, tenant_id: str) -> None:
    default_agent = session.get(AgentProfile, f"agent_{tenant_id}_default")
    if not default_agent:
        return
    metadata = dict(default_agent.metadata_json or {})
    if metadata and not (
        metadata.get("is_default_employee") is True
        or metadata.get("created_by") == "admin"
        or metadata.get("owner_user_id") == "admin"
    ):
        return
    metadata.update(
        {
            "is_default_employee": True,
            "hidden_from_staffdeck": True,
            "archived_by_seed": True,
        }
    )
    default_agent.metadata_json = _system_seed_metadata(metadata)
    default_agent.status = "archived"
    default_agent.updated_at = utc_now()
    session.add(default_agent)


def _system_seed_metadata(extra: dict[str, object] | None = None) -> dict[str, object]:
    metadata = dict(extra or {})
    metadata.update(
        {
            "owner_user_id": "admin",
            "owner_username": "admin",
            "owner_display_name": "Administrator",
            "created_by_user_id": "admin",
            "created_by_username": "admin",
            "created_by": "admin",
            "created_by_display_name": "Administrator",
            "creator_name": "admin",
        }
    )
    return metadata


def _tool_config_with_base_url(tool_config: dict, base_url: str) -> dict:
    config = dict(tool_config)
    config["url"] = _tool_url_with_base(config["url"], base_url)
    return config


def _tool_url_with_base(url: str, base_url: str) -> str:
    stripped = url.strip()
    if stripped.startswith("/"):
        return f"{base_url}{stripped}"
    return stripped


def _seed_weather_general_skill(session: Session) -> None:
    folder_source = Path("/Users/hm/Downloads/maomao-weather-1.0.2")
    file_source = Path("/Users/hm/Downloads/SKILL.md")
    package_files = _collect_general_skill_folder(folder_source) if folder_source.exists() else []
    source = folder_source / "SKILL.md" if package_files else file_source
    if not source.exists():
        return
    try:
        markdown = source.read_text(encoding="utf-8").strip()
    except OSError:
        return
    if not markdown:
        return
    slug = "weather-zh"
    existing = session.exec(
        select(GeneralSkill).where(
            GeneralSkill.tenant_id == "tenant_demo",
            GeneralSkill.slug == slug,
        )
    ).first()
    if existing:
        needs_package_backfill = package_files and not (existing.skill_files_json or [])
        if (
            existing.skill_markdown != markdown
            or existing.status != "published"
            or needs_package_backfill
        ):
            existing.name = existing.name or "中国城市天气"
            existing.description = existing.description or "中国城市天气查询工具"
            existing.homepage = existing.homepage or "https://www.weather.com.cn/"
            existing.skill_markdown = markdown
            if package_files:
                existing.skill_files_json = package_files
                existing.metadata_json = existing.metadata_json or {
                    "source": "maomao-weather-1.0.2"
                }
            existing.status = "published"
            existing.permissions_json = existing.permissions_json or {
                "network": True,
                "python": True,
            }
            existing.runtime_config_json = existing.runtime_config_json or {
                "runtime": "bash",
                "timeout_seconds": 12,
            }
            existing.updated_at = utc_now()
        return
    session.add(
        GeneralSkill(
            tenant_id="tenant_demo",
            slug=slug,
            name="中国城市天气",
            description="中国城市天气查询工具",
            homepage="https://www.weather.com.cn/",
            skill_markdown=markdown,
            skill_files_json=package_files,
            metadata_json={"source": "maomao-weather-1.0.2"} if package_files else {},
            status="published",
            permissions_json={"network": True, "python": True},
            runtime_config_json={
                "runtime": "bash" if package_files else "python",
                "timeout_seconds": 12,
            },
        )
    )


def _collect_general_skill_folder(folder: Path) -> list[dict[str, object]]:
    skill_file = folder / "SKILL.md"
    if not skill_file.exists():
        return []
    files: list[dict[str, object]] = []
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        except OSError:
            continue
        relative = path.relative_to(folder).as_posix()
        files.append(
            {
                "path": relative,
                "content": content,
                "size": len(content.encode("utf-8")),
                "mime_type": "text/markdown" if relative.lower().endswith(".md") else "text/plain",
            }
        )
    return files


def _sync_demo_skill_if_stale(existing: Skill, desired: dict) -> None:
    content = _skill_content_graph(dict(existing.content_json or {}))
    desired = _skill_content_graph(desired)
    changed = False
    current_nodes = [node for node in content.get("nodes", []) if isinstance(node, dict)]
    desired_nodes = [node for node in desired.get("nodes", []) if isinstance(node, dict)]
    current_nodes_by_id = {str(node.get("node_id") or ""): node for node in current_nodes}
    merged_nodes: list[dict] = []
    used_node_ids: set[str] = set()

    for desired_node in desired_nodes:
        node_id = str(desired_node.get("node_id") or "")
        current_node = current_nodes_by_id.get(node_id)
        if not current_node:
            merged_nodes.append(dict(desired_node))
            used_node_ids.add(node_id)
            changed = True
            continue
        desired_instruction = str(desired_node.get("instruction") or "")
        current_instruction = str(current_node.get("instruction") or "")
        if desired_instruction and not current_instruction:
            current_node["instruction"] = desired_instruction
            changed = True
        for key in (
            "type",
            "name",
            "expected_user_info",
            "allowed_actions",
            "knowledge_scope",
            "retry_policy",
            "optional",
            "condition",
            "metadata",
        ):
            if key in desired_node and current_node.get(key) != desired_node.get(key):
                current_node[key] = desired_node[key]
                changed = True
        merged_nodes.append(current_node)
        used_node_ids.add(node_id)

    for current_node in current_nodes:
        node_id = str(current_node.get("node_id") or "")
        if node_id and node_id not in used_node_ids:
            merged_nodes.append(current_node)
            used_node_ids.add(node_id)

    if desired_nodes and content.get("nodes") != merged_nodes:
        content["nodes"] = merged_nodes
        changed = True

    for graph_key in ("edges", "start_node_id", "terminal_node_ids"):
        if graph_key in desired and content.get(graph_key) != desired.get(graph_key):
            content[graph_key] = desired[graph_key]
            changed = True

    if desired.get("required_info") and content.get("required_info") != desired.get(
        "required_info"
    ):
        content["required_info"] = desired["required_info"]
        changed = True

    if desired.get("interruption_policy") and content.get("interruption_policy") != desired.get(
        "interruption_policy"
    ):
        content["interruption_policy"] = desired["interruption_policy"]
        changed = True
    if desired.get("slot_filling_policy"):
        merged_policy = _merge_slot_filling_policy(
            content.get("slot_filling_policy"), desired["slot_filling_policy"]
        )
        if content.get("slot_filling_policy") != merged_policy:
            content["slot_filling_policy"] = merged_policy
            changed = True
    if desired.get("response_rules"):
        merged_rules = _append_missing_rules(
            content.get("response_rules"), desired["response_rules"]
        )
        if content.get("response_rules") != merged_rules:
            content["response_rules"] = merged_rules
            changed = True

    if changed:
        existing.content_json = content
        existing.updated_at = utc_now()


def _skill_content_graph(content: dict) -> dict:
    next_content = dict(content or {})
    nodes = next_content.get("nodes") if isinstance(next_content.get("nodes"), list) else []
    node_ids = [str(node.get("node_id") or "") for node in nodes if isinstance(node, dict)]
    node_ids = [node_id for node_id in node_ids if node_id]
    if node_ids:
        next_content.setdefault("start_node_id", node_ids[0])
        next_content.setdefault("terminal_node_ids", [node_ids[-1]])
        next_content.setdefault(
            "edges",
            [
                {
                    "source_node_id": source,
                    "next_node_id": target,
                    "condition": "",
                    "priority": index,
                    "label": "",
                }
                for index, (source, target) in enumerate(zip(node_ids, node_ids[1:]))
            ],
        )
    else:
        next_content.setdefault("nodes", [])
        next_content.setdefault("edges", [])
        next_content.setdefault("start_node_id", "")
        next_content.setdefault("terminal_node_ids", [])
    return next_content


def _merge_slot_filling_policy(current: object, desired: dict) -> dict:
    current_policy = dict(current) if isinstance(current, dict) else {}
    merged = {**current_policy, **desired}
    target_info = {str(item) for item in current_policy.get("target_info", []) if str(item).strip()}
    target_info.update(str(item) for item in desired.get("target_info", []) if str(item).strip())
    merged["target_info"] = sorted(target_info)
    return merged


def _append_missing_rules(current: object, desired: list[str]) -> list[str]:
    rules = [str(item) for item in current] if isinstance(current, list) else []
    for rule in desired:
        if rule not in rules:
            rules.append(rule)
    return rules
