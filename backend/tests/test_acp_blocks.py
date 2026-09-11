"""Tests for message-level atomic blocks and the block store."""

from app.core.acp.blocks import Block, BlockStore


def test_each_message_becomes_one_block_with_sequential_ids() -> None:
    store = BlockStore()
    first = store.add("m1", "hello")
    second = store.add("m2", "world")
    assert first.block_id == 1
    assert second.block_id == 2
    assert first.message_id == "m1"
    assert first.content == "hello"
    assert not first.is_summary
    assert first.tier == 1


def test_add_many_creates_one_block_per_message() -> None:
    store = BlockStore()
    blocks = store.add_many([("m1", "a"), ("m2", "b"), ("m3", "c")])
    assert [block.block_id for block in blocks] == [1, 2, 3]
    assert len(store) == 3


def test_empty_content_block_is_allowed() -> None:
    store = BlockStore()
    block = store.add("m1", "")
    assert block.content == ""
    assert len(store) == 1


def test_skip_flag_is_recorded_on_block() -> None:
    store = BlockStore()
    skipped = store.add("m1", "system instruction", skip=True)
    normal = store.add("m2", "user message")
    assert skipped.skip
    assert not normal.skip


def test_get_returns_none_for_missing_block() -> None:
    store = BlockStore()
    store.add("m1", "hello")
    assert store.get(99) is None


def test_replace_range_returns_removed_blocks() -> None:
    store = BlockStore()
    store.add_many([("m1", "a"), ("m2", "b"), ("m3", "c")])
    replacement = Block(block_id=4, message_id="acp_summary_1", content="abc", is_summary=True)
    removed = store.replace(0, 2, replacement)
    assert [block.block_id for block in removed] == [1, 2, 3]
    assert store.all() == (replacement,)


def test_replace_by_id_swaps_block_in_place() -> None:
    store = BlockStore()
    store.add_many([("m1", "a"), ("m2", "b")])
    summary = Block(
        block_id=3,
        message_id="acp_summary_1",
        content="ab",
        is_summary=True,
        checkpoint_id=1,
    )
    store.replace(0, 1, summary)
    assert store.all() == (summary,)
    removed = store.replace_by_id(
        3,
        [
            Block(block_id=1, message_id="m1", content="a"),
            Block(block_id=2, message_id="m2", content="b"),
        ],
    )
    assert removed is not None
    assert removed.block_id == 3
    assert [block.block_id for block in store.all()] == [1, 2]


def test_next_id_tracks_allocations() -> None:
    store = BlockStore()
    assert store.next_id() == 1
    store.add("m1", "a")
    assert store.next_id() == 2