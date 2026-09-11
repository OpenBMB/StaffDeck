"""Tests for checkpoint records and the checkpoint store."""

import pytest

from app.core.acp.blocks import Block
from app.core.acp.checkpoint import CheckpointRecord, CheckpointStore


def _record(checkpoint_id: int = 1) -> CheckpointRecord:
    return CheckpointRecord(
        checkpoint_id=checkpoint_id,
        seq_start=0,
        seq_end=2,
        summary_block_id=4,
        tier=1,
        original_blocks=(
            Block(block_id=1, message_id="m1", content="a"),
            Block(block_id=2, message_id="m2", content="b"),
            Block(block_id=3, message_id="m3", content="c"),
        ),
        token_delta=10,
    )


def test_checkpoint_record_holds_seq_mapping_and_boundary_info() -> None:
    record = _record()
    assert record.seq_start == 0
    assert record.seq_end == 2
    assert record.summary_block_id == 4
    assert record.tier == 1
    assert [block.block_id for block in record.original_blocks] == [1, 2, 3]


def test_checkpoint_store_add_get_round_trip() -> None:
    store = CheckpointStore()
    store.add(_record())
    record = store.get(1)
    assert record is not None
    assert record.checkpoint_id == 1
    assert store.get(99) is None


def test_find_by_summary_block() -> None:
    store = CheckpointStore()
    store.add(_record())
    record = store.find_by_summary_block(4)
    assert record is not None
    assert record.checkpoint_id == 1
    assert store.find_by_summary_block(99) is None


def test_checkpoint_ids_increment() -> None:
    store = CheckpointStore()
    assert store.next_id() == 1
    store.add(_record(1))
    assert store.next_id() == 2


def test_checkpoint_records_are_immutable() -> None:
    record = _record()
    with pytest.raises(AttributeError):
        record.tier = 2  # type: ignore[misc]


def test_checkpoint_store_is_append_only() -> None:
    store = CheckpointStore()
    store.add(_record(1))
    store.add(_record(2))
    assert len(store) == 2
    assert [record.checkpoint_id for record in store.all()] == [1, 2]