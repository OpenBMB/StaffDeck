"""Integration tests for the AcpEngine facade."""

from app.core.acp import AcpEngine, AcpError, CompressResult, DecompressResult, SearchResult


def test_compress_decompress_round_trip_restores_original() -> None:
    engine = AcpEngine()
    engine.add_messages([("m1", "alpha"), ("m2", "beta"), ("m3", "gamma")])
    result = engine.compress(0, 2, "summary of alpha beta gamma")
    assert isinstance(result, CompressResult)
    assert result.tier == 1
    assert result.removed_block_ids == (1, 2, 3)
    assert engine.blocks() == (engine.get_block(result.summary_block_id),)
    restored = engine.decompress(result.summary_block_id)
    assert isinstance(restored, DecompressResult)
    assert restored.restored_block_ids == (1, 2, 3)
    assert [block.content for block in engine.blocks()] == ["alpha", "beta", "gamma"]


def test_compress_invalid_range_returns_structured_error() -> None:
    engine = AcpEngine()
    engine.add_messages([("m1", "alpha")])
    result = engine.compress(1, 2, "summary")
    assert isinstance(result, AcpError)
    assert result.code == "invalid_range"
    result = engine.compress(2, 1, "summary")
    assert isinstance(result, AcpError)
    assert result.code == "invalid_range"
    result = engine.compress(-1, 0, "summary")
    assert isinstance(result, AcpError)
    assert result.code == "invalid_range"


def test_compress_single_message_block() -> None:
    engine = AcpEngine()
    engine.add_messages([("m1", "alpha"), ("m2", "beta")])
    result = engine.compress(1, 1, "beta summary")
    assert isinstance(result, CompressResult)
    assert result.removed_block_ids == (2,)
    assert [block.message_id for block in engine.blocks()] == ["m1", "acp_summary_1"]


def test_compress_range_with_skipped_block_returns_error() -> None:
    engine = AcpEngine()
    engine.add_message("m1", "system instruction", skip=True)
    engine.add_message("m2", "user message")
    result = engine.compress(0, 1, "summary")
    assert isinstance(result, AcpError)
    assert result.code == "block_skipped"


def test_compress_empty_content_block() -> None:
    engine = AcpEngine()
    engine.add_message("m1", "")
    engine.add_message("m2", "beta")
    result = engine.compress(0, 1, "summary")
    assert isinstance(result, CompressResult)
    assert result.removed_block_ids == (1, 2)


def test_decompress_non_summary_block_returns_error() -> None:
    engine = AcpEngine()
    engine.add_message("m1", "alpha")
    result = engine.decompress(1)
    assert isinstance(result, AcpError)
    assert result.code == "not_a_summary"


def test_decompress_missing_block_returns_error() -> None:
    engine = AcpEngine()
    result = engine.decompress(99)
    assert isinstance(result, AcpError)
    assert result.code == "block_not_found"


def test_compress_search_decompress_closed_loop() -> None:
    engine = AcpEngine()
    engine.add_messages(
        [("m1", "退款政策 refund policy"), ("m2", "发货时间 shipping schedule")]
    )
    result = engine.compress(0, 1, "历史消息摘要：退款与发货")
    assert isinstance(result, CompressResult)
    search = engine.search_context("退款")
    assert isinstance(search, SearchResult)
    assert search.matched
    assert search.hits[0].block_id == result.summary_block_id
    restored = engine.decompress(result.summary_block_id)
    assert isinstance(restored, DecompressResult)
    assert [block.content for block in engine.blocks()] == [
        "退款政策 refund policy",
        "发货时间 shipping schedule",
    ]


def test_search_hits_hidden_original_content() -> None:
    engine = AcpEngine()
    engine.add_messages([("m1", "量子纠缠实验数据"), ("m2", "普通对话内容")])
    result = engine.compress(0, 1, "历史消息摘要")
    assert isinstance(result, CompressResult)
    search = engine.search_context("量子纠缠")
    assert isinstance(search, SearchResult)
    assert search.matched
    assert search.hits[0].block_id == result.summary_block_id
    assert search.hits[0].source == "hidden"


def test_nudge_via_engine() -> None:
    engine = AcpEngine()
    assert engine.nudge(1000) is None
    recommendation = engine.nudge(int(128000 * 0.8))
    assert recommendation is not None
    assert recommendation.level == "normal"


def test_status_via_engine() -> None:
    engine = AcpEngine()
    engine.add_messages([("m1", "alpha"), ("m2", "beta")])
    engine.compress(0, 1, "summary")
    status = engine.status()
    assert status.total_blocks == 1
    assert status.ledger_balance >= 0


def test_ledger_never_negative_across_multiple_compressions() -> None:
    """Regression for upstream issue #54: mixed estimates must not drive the ledger negative."""

    class ErraticMeter:
        def __init__(self) -> None:
            self._calls = 0

        def estimate_tokens(self, text: str) -> int:
            self._calls += 1
            return 10 if self._calls % 2 else 100000

    engine = AcpEngine(meter=ErraticMeter())
    engine.add_messages([(f"m{i}", f"message content {i}") for i in range(6)])
    for _ in range(3):
        result = engine.compress(0, 1, "summary")
        assert isinstance(result, CompressResult)
        assert result.ledger_balance >= 0
        restored = engine.decompress(result.summary_block_id)
        assert isinstance(restored, DecompressResult)
        assert restored.ledger_balance >= 0
    assert engine.ledger.never_negative


def test_to_state_from_state_round_trip_preserves_engine() -> None:
    engine = AcpEngine()
    engine.add_messages([("m1", "alpha"), ("m2", "beta"), ("m3", "gamma")])
    result = engine.compress(0, 1, "summary of alpha beta")
    assert isinstance(result, CompressResult)
    state = engine.to_state()

    restored = AcpEngine()
    restored.from_state(state)

    assert [block.message_id for block in restored.blocks()] == [
        "acp_summary_1",
        "m3",
    ]
    assert restored.blocks()[0].is_summary is True
    assert restored.blocks()[0].checkpoint_id == 1
    assert restored._store.next_id() == engine._store.next_id()
    assert restored._checkpoints.next_id() == engine._checkpoints.next_id()
    assert restored.ledger.balance == engine.ledger.balance
    checkpoint = restored._checkpoints.get(1)
    assert checkpoint is not None
    assert [block.content for block in checkpoint.original_blocks] == ["alpha", "beta"]
    decompressed = restored.decompress(result.summary_block_id)
    assert isinstance(decompressed, DecompressResult)
    assert [block.content for block in restored.blocks()] == ["alpha", "beta", "gamma"]


def test_to_state_max_originals_evicts_old_checkpoint_originals() -> None:
    engine = AcpEngine()
    engine.add_messages([(f"m{i}", f"content {i}") for i in range(8)])
    for index in range(3):
        result = engine.compress(0, 1, f"summary {index}")
        assert isinstance(result, CompressResult)

    state = engine.to_state(max_originals=2)
    checkpoints = state["checkpoints"]
    assert len(checkpoints) == 3
    assert checkpoints[0]["originals_evicted"] is True
    assert checkpoints[0]["original_blocks"] == []
    assert checkpoints[1]["originals_evicted"] is False
    assert len(checkpoints[1]["original_blocks"]) == 2
    assert checkpoints[2]["originals_evicted"] is False
    assert len(checkpoints[2]["original_blocks"]) == 2

    restored = AcpEngine()
    restored.from_state(state)
    assert restored._checkpoints.get(1).original_blocks == ()
    assert len(restored._checkpoints.get(3).original_blocks) == 2


def test_decompress_evicted_checkpoint_returns_error_and_keeps_summary() -> None:
    # Simulate a serialized state where the checkpoint's originals were
    # evicted by the originals cap: the summary block stays visible but the
    # checkpoint carries no original blocks.
    state = {
        "blocks": [
            {
                "block_id": 1,
                "message_id": "acp_summary_1",
                "content": "摘要",
                "tier": 1,
                "is_summary": True,
                "checkpoint_id": 1,
                "skip": False,
            }
        ],
        "checkpoints": [
            {
                "checkpoint_id": 1,
                "seq_start": 0,
                "seq_end": 1,
                "summary_block_id": 1,
                "tier": 1,
                "token_delta": 0,
                "created_at": 0.0,
                "originals_evicted": True,
                "original_blocks": [],
            }
        ],
        "next_block_id": 2,
        "next_checkpoint_id": 2,
        "ledger_balance": 0,
        "ledger_warnings": [],
    }
    restored = AcpEngine()
    restored.from_state(state)

    result = restored.decompress(1)
    assert isinstance(result, AcpError)
    assert result.code == "CHECKPOINT_ORIGINALS_EVICTED"
    assert restored.blocks()[0].block_id == 1
    assert restored.blocks()[0].is_summary is True


def test_to_state_is_json_serializable() -> None:
    import json

    engine = AcpEngine()
    engine.add_messages([("m1", "alpha"), ("m2", "beta")])
    engine.compress(0, 1, "summary")
    state = engine.to_state(max_originals=1)
    round_tripped = json.loads(json.dumps(state))
    assert round_tripped["blocks"][0]["is_summary"] is True
    assert round_tripped["checkpoints"][0]["originals_evicted"] is False