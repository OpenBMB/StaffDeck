from app.core.conversation_context import (
    ConversationContextSettings,
    build_conversation_context,
)


def test_conversation_context_keeps_full_history_under_budget() -> None:
    messages = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "您好"},
        {"role": "user", "content": "我是 hx，我要买 A2"},
        {"role": "assistant", "content": "请问买几个？"},
        {"role": "user", "content": "买两个"},
    ]

    context = build_conversation_context(messages, token_budget=1_000)

    assert context["messages"] == messages
    assert context["metadata"]["compacted"] is False
    assert context["metadata"]["total_messages"] == 5
    assert context["metadata"]["omitted_messages"] == 0


def test_conversation_context_compacts_only_after_budget_is_exceeded() -> None:
    messages = [
        {"role": "user", "content": f"old user message {index} " + "x" * 80}
        if index % 2 == 0
        else {"role": "assistant", "content": f"old assistant message {index} " + "y" * 80}
        for index in range(20)
    ]

    context = build_conversation_context(messages, token_budget=500)
    projected = context["messages"]

    assert context["metadata"]["compacted"] is True
    assert context["metadata"]["omitted_messages"] > 0
    assert projected[0]["role"] == "user"
    assert "历史的信息可以被总结为" in projected[0]["content"]
    assert "近期的历史信息总结为" in projected[1]["content"]
    assert projected[-1]["content"] == messages[-1]["content"]
    assert context["metadata"]["estimated_tokens"] <= 500


def test_context_rotates_medium_history_into_long_history_on_next_threshold() -> None:
    messages = [
        {
            "id": f"message_{index}",
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"round {index} " + ("中" * 120),
            "created_at": f"2026-07-13T12:{index:02d}:00",
        }
        for index in range(20)
    ]
    summaries: list[tuple[str, str]] = []

    def summarize(label: str, source: str, _budget: int) -> str:
        summaries.append((label, source))
        return f"{label}摘要：{source[:120]}"

    first = build_conversation_context(
        messages, token_budget=700, summary_builder=summarize
    )
    first_state = first["context_state"]

    assert first_state["compaction_count"] == 1
    assert first_state["long_term_summary"] == ""
    assert first_state["medium_term_summary"].startswith("近期历史信息摘要")
    assert first["messages"][0]["content"].startswith("历史的信息可以被总结为：")
    assert first["messages"][1]["content"].startswith("近期的历史信息总结为：")

    more_messages = [
        *messages,
        *[
            {
                "id": f"message_{index}",
                "role": "user" if index % 2 == 0 else "assistant",
                "content": f"new round {index} " + ("新" * 120),
                "created_at": f"2026-07-13T13:{index - 20:02d}:00",
            }
            for index in range(20, 36)
        ],
    ]
    second = build_conversation_context(
        more_messages,
        token_budget=700,
        context_state=first_state,
        summary_builder=summarize,
    )
    second_state = second["context_state"]

    assert second_state["compaction_count"] == 2
    assert second_state["long_term_summary"].startswith("长期历史信息摘要")
    assert first_state["medium_term_summary"] in summaries[-2][1]
    assert second_state["medium_term_summary"].startswith("近期历史信息摘要")
    assert second["metadata"]["current_turn_time"] == "2026-07-13T13:15:00"
    assert second["metadata"]["estimated_tokens"] <= 700


def test_conversation_context_uses_runtime_compaction_settings() -> None:
    messages = [
        {
            "id": f"message_{index}",
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"message {index} " + ("内容" * 80),
        }
        for index in range(12)
    ]
    summary_budgets: list[int] = []

    def summarize(_label: str, source: str, budget: int) -> str:
        summary_budgets.append(budget)
        return source

    settings = ConversationContextSettings(
        token_budget=1_000,
        compaction_trigger_ratio=0.20,
        recent_round_limit=1,
        long_summary_token_budget=160,
        medium_summary_token_budget=180,
        allowed_roles=frozenset({"user"}),
        long_summary_prefix="长期上下文：",
        medium_summary_prefix="近期上下文：",
    )
    context = build_conversation_context(
        messages,
        settings=settings,
        summary_builder=summarize,
    )

    assert context["metadata"]["token_budget"] == 1_000
    assert context["metadata"]["compaction_trigger_ratio"] == 0.20
    assert context["metadata"]["compaction_trigger_tokens"] == 200
    assert context["metadata"]["recent_round_limit"] == 1
    assert context["metadata"]["long_summary_token_budget"] == 160
    assert context["metadata"]["medium_summary_token_budget"] == 180
    assert context["metadata"]["allowed_roles"] == ["user"]
    assert context["metadata"]["long_summary_prefix"] == "长期上下文："
    assert context["metadata"]["medium_summary_prefix"] == "近期上下文："
    assert all(message["role"] == "user" for message in context["messages"])
    assert context["messages"][0]["content"].startswith("长期上下文：")
    assert context["messages"][1]["content"].startswith("近期上下文：")
    assert summary_budgets == [180]


def test_conversation_context_compacts_assistant_only_history() -> None:
    context = build_conversation_context(
        [
            {
                "id": f"assistant_{index}",
                "role": "assistant",
                "content": f"assistant message {index} " + ("内容" * 80),
            }
            for index in range(8)
        ],
        settings=ConversationContextSettings(
            token_budget=600,
            compaction_trigger_ratio=0.20,
            recent_round_limit=2,
            long_summary_token_budget=128,
            medium_summary_token_budget=128,
            allowed_roles=frozenset({"assistant"}),
        ),
    )

    assert context["metadata"]["compacted_now"] is True
    assert context["metadata"]["allowed_roles"] == ["assistant"]
    assert context["context_state"]["summarized_through_message_id"] == "assistant_5"
    assert [message["role"] for message in context["messages"][-2:]] == [
        "assistant",
        "assistant",
    ]
