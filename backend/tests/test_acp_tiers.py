"""Tests for tiered distillation of summary nodes."""

import pytest

from app.core.acp import AcpEngine, AcpError, CompressResult
from app.core.acp.tiers import MAX_TIER, can_distill, next_tier, tier_label


def test_next_tier_progression() -> None:
    assert next_tier(1) == 2
    assert next_tier(2) == 3


def test_next_tier_rejects_invalid_input() -> None:
    with pytest.raises(ValueError):
        next_tier(0)
    with pytest.raises(ValueError):
        next_tier(MAX_TIER)


def test_can_distill() -> None:
    assert can_distill(1)
    assert can_distill(2)
    assert not can_distill(3)
    assert not can_distill(0)


def test_tier_label() -> None:
    assert tier_label(2) == "tier2"


def test_recompressing_summary_produces_tier2_then_tier3() -> None:
    engine = AcpEngine()
    engine.add_messages([("m1", "alpha"), ("m2", "beta"), ("m3", "gamma")])
    first = engine.compress(0, 2, "summary one")
    assert isinstance(first, CompressResult)
    assert first.tier == 1
    second = engine.compress(0, 0, "summary two")
    assert isinstance(second, CompressResult)
    assert second.tier == 2
    third = engine.compress(0, 0, "summary three")
    assert isinstance(third, CompressResult)
    assert third.tier == 3
    block = engine.get_block(third.summary_block_id)
    assert block is not None
    assert block.tier == 3
    assert block.is_summary


def test_original_checkpoints_preserved_after_distillation() -> None:
    engine = AcpEngine()
    engine.add_messages([("m1", "alpha"), ("m2", "beta")])
    first = engine.compress(0, 1, "s1")
    second = engine.compress(0, 0, "s2")
    assert isinstance(first, CompressResult)
    assert isinstance(second, CompressResult)
    status = engine.status()
    assert len(status.checkpoints) == 2
    assert [checkpoint.tier for checkpoint in status.checkpoints] == [1, 2]


def test_distillation_beyond_max_tier_returns_structured_error() -> None:
    engine = AcpEngine()
    engine.add_messages([("m1", "alpha")])
    for _ in range(3):
        result = engine.compress(0, 0, "summary")
        assert isinstance(result, CompressResult)
    result = engine.compress(0, 0, "summary again")
    assert isinstance(result, AcpError)
    assert result.code == "tier_limit"


def test_distilled_chain_decompresses_level_by_level() -> None:
    engine = AcpEngine()
    engine.add_messages([("m1", "alpha"), ("m2", "beta")])
    first = engine.compress(0, 1, "tier1 summary")
    second = engine.compress(0, 0, "tier2 summary")
    assert isinstance(first, CompressResult)
    assert isinstance(second, CompressResult)
    restored = engine.decompress(second.summary_block_id)
    assert restored is not None and not isinstance(restored, AcpError)
    assert restored.restored_block_ids == (first.summary_block_id,)
    assert [block.content for block in engine.blocks()] == ["tier1 summary"]