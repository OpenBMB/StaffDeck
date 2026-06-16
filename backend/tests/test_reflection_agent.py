from app.core.reflection_agent import ReflectionAgent
from app.db.models import Skill, Tool


def test_reflection_tool_payload_drops_stale_skill_references() -> None:
    agent = ReflectionAgent()
    stale_tool = Tool(
        tenant_id="tenant_demo",
        name="product.price_query",
        display_name="商品价格查询",
        method="POST",
        url="http://localhost:8000/api/mock/product/price-query",
        allowed_skills_json=["missing_price_compare"],
        enabled=True,
    )

    payload = agent._available_tool_payload([stale_tool], {"skill_purchase_001"})

    assert payload == []


def test_reflection_tool_payload_keeps_existing_allowed_skill_only() -> None:
    agent = ReflectionAgent()
    price_skill = Skill(
        tenant_id="tenant_demo",
        skill_id="skill_price_compare_001",
        name="商品比价服务",
        content_json={"skill_id": "skill_price_compare_001"},
        status="published",
    )
    tool = Tool(
        tenant_id="tenant_demo",
        name="product.price_query",
        display_name="商品价格查询",
        description="根据商品名称查询价格。",
        method="POST",
        url="http://localhost:8000/api/mock/product/price-query",
        input_schema={"type": "object"},
        allowed_skills_json=[price_skill.skill_id, "missing_skill"],
        enabled=True,
    )

    payload = agent._available_tool_payload([tool], {price_skill.skill_id})

    assert payload == [
        {
            "name": "product.price_query",
            "display_name": "商品价格查询",
            "description": "根据商品名称查询价格。",
            "bucket": "未分桶",
            "input_schema": {"type": "object"},
            "allowed_skills": ["skill_price_compare_001"],
        }
    ]
