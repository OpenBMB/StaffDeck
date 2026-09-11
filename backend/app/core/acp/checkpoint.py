"""Checkpoint records enabling recovery of compressed content.

Every compress operation writes one checkpoint holding the sequence
mapping (compressed range), the summary block id, and the original blocks
with their full content. Checkpoints are immutable and never deleted, so
decompression and audit trails stay possible at any depth.
"""

import time
from dataclasses import dataclass, field

from .blocks import Block


@dataclass(frozen=True)
class CheckpointRecord:
    """Immutable record of one compress operation."""

    checkpoint_id: int
    seq_start: int
    seq_end: int
    summary_block_id: int
    tier: int
    original_blocks: tuple[Block, ...]
    token_delta: int
    created_at: float = field(default_factory=time.time)


class CheckpointStore:
    """Append-only store of checkpoint records."""

    def __init__(self) -> None:
        self._records: dict[int, CheckpointRecord] = {}
        self._next_id = 1

    def add(self, record: CheckpointRecord) -> CheckpointRecord:
        self._records[record.checkpoint_id] = record
        self._next_id = max(self._next_id, record.checkpoint_id + 1)
        return record

    def get(self, checkpoint_id: int) -> CheckpointRecord | None:
        return self._records.get(checkpoint_id)

    def find_by_summary_block(self, block_id: int) -> CheckpointRecord | None:
        for record in self._records.values():
            if record.summary_block_id == block_id:
                return record
        return None

    def all(self) -> tuple[CheckpointRecord, ...]:
        return tuple(self._records.values())

    def next_id(self) -> int:
        return self._next_id

    def __len__(self) -> int:
        return len(self._records)