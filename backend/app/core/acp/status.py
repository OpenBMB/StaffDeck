"""Status report structure for the acp_status operation."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class BlockLedgerEntry:
    """One block's entry in the status ledger."""

    block_id: int
    message_id: str
    tier: int
    is_summary: bool
    content_length: int


@dataclass(frozen=True)
class CheckpointMappingEntry:
    """Checkpoint mapping surfaced in the status report."""

    checkpoint_id: int
    seq_start: int
    seq_end: int
    summary_block_id: int
    tier: int


@dataclass(frozen=True)
class ContextStatus:
    """Snapshot of the compression ledger and block state."""

    total_blocks: int
    total_chars: int
    summary_blocks: int
    original_blocks: int
    tier_counts: dict[int, int] = field(default_factory=dict)
    ledger_balance: int = 0
    ledger_warnings: tuple[str, ...] = ()
    blocks: tuple[BlockLedgerEntry, ...] = ()
    checkpoints: tuple[CheckpointMappingEntry, ...] = ()