"""ConversationContextSettings 归一化的防回归测试。

线上事故（2026-09-08）：ui_configs 脏数据 token_budget=0 → 归一化 clamp 成
1 token → 每轮对话都触发 context.compact（2 次 LLM 调用 ~30s）。归一化必须
把 ≤0 的配置视为未配置并回退默认值，而不是 clamp 到 1。
"""

from app.core.base.conversation_context import (
    COMPACTION_TRIGGER_RATIO,
    DEFAULT_CONTEXT_TOKEN_BUDGET,
    ConversationContextSettings,
)


def test_zero_budget_falls_back_to_default_instead_of_one_token() -> None:
    settings = ConversationContextSettings(
        token_budget=0,
        compaction_trigger_ratio=0.0,
        recent_round_limit=0,
        long_summary_token_budget=0,
        medium_summary_token_budget=0,
    ).normalized()

    assert settings.token_budget == DEFAULT_CONTEXT_TOKEN_BUDGET
    assert settings.compaction_trigger_ratio == COMPACTION_TRIGGER_RATIO
    assert settings.recent_round_limit > 1
    assert settings.long_summary_token_budget > 1
    assert settings.medium_summary_token_budget > 1


def test_valid_settings_are_preserved() -> None:
    settings = ConversationContextSettings(
        token_budget=8_000,
        compaction_trigger_ratio=0.8,
        recent_round_limit=4,
        long_summary_token_budget=2_000,
        medium_summary_token_budget=1_000,
    ).normalized()

    assert settings.token_budget == 8_000
    assert settings.compaction_trigger_ratio == 0.8
    assert settings.recent_round_limit == 4
    assert settings.long_summary_token_budget == 2_000
    assert settings.medium_summary_token_budget == 1_000


def test_zero_budget_does_not_trigger_compaction_on_small_talk() -> None:
    """6 条小消息 + budget=0 脏数据：修复后不应触发压缩。"""
    from app.core.base.conversation_context import build_conversation_context

    messages = [
        {"role": "user", "content": "你好", "_message_id": f"m{i}a", "_created_at": "2026-09-08T00:00:00"}
        if i % 2 == 0
        else {"role": "assistant", "content": "你好呀！", "_message_id": f"m{i}a", "_created_at": "2026-09-08T00:00:00"}
        for i in range(6)
    ]
    result = build_conversation_context(
        messages,
        settings=ConversationContextSettings(
            token_budget=0, compaction_trigger_ratio=0.0, recent_round_limit=0
        ),
        context_state={},
        summary_builder=lambda *args: "不应被调用",
    )

    assert result["metadata"]["compacted_now"] is False
    assert result["metadata"]["token_budget"] == DEFAULT_CONTEXT_TOKEN_BUDGET
