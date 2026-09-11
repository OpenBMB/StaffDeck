"""Tests for the acp_status report structure."""

from app.core.acp import AcpEngine, CompressResult


def test_status_reports_initial_state() -> None:
    engine = AcpEngine()
    engine.add_messages([("m1", "alpha"), ("m2", "beta")])
    status = engine.status()
    assert status.total_blocks == 2
    assert status.total_chars == 9
    assert status.summary_blocks == 0
    assert status.original_blocks == 2
    assert status.tier_counts == {1: 2}
    assert status.ledger_balance == 0
    assert status.ledger_warnings == ()


def test_status_reports_compression_ledger() -> None:
    engine = AcpEngine()
    engine.add_messages([("m1", "alpha"), ("m2", "beta"), ("m3", "gamma")])
    result = engine.compress(0, 2, "summary")
    assert isinstance(result, CompressResult)
    status = engine.status()
    assert status.total_blocks == 1
    assert status.summary_blocks == 1
    assert status.original_blocks == 0
    assert status.tier_counts == {1: 1}
    assert status.ledger_balance == result.ledger_balance


def test_status_lists_block_ledger_entries() -> None:
    engine = AcpEngine()
    engine.add_messages([("m1", "alpha")])
    status = engine.status()
    assert len(status.blocks) == 1
    entry = status.blocks[0]
    assert entry.block_id == 1
    assert entry.message_id == "m1"
    assert entry.tier == 1
    assert not entry.is_summary
    assert entry.content_length == 5


def test_status_lists_checkpoint_mapping() -> None:
    engine = AcpEngine()
    engine.add_messages([("m1", "alpha"), ("m2", "beta")])
    result = engine.compress(0, 1, "summary")
    assert isinstance(result, CompressResult)
    status = engine.status()
    assert len(status.checkpoints) == 1
    mapping = status.checkpoints[0]
    assert mapping.checkpoint_id == result.checkpoint_id
    assert mapping.seq_start == 0
    assert mapping.seq_end == 1
    assert mapping.summary_block_id == result.summary_block_id
    assert mapping.tier == 1


def test_status_surfaces_ledger_warnings() -> None:
    engine = AcpEngine()
    engine.add_messages([("m1", "alpha"), ("m2", "beta")])
    result = engine.compress(0, 1, "summary")
    assert isinstance(result, CompressResult)
    engine.decompress(result.summary_block_id)
    status = engine.status()
    assert status.ledger_balance >= 0
    assert status.ledger_warnings
    assert any("clamped" in warning for warning in status.ledger_warnings)