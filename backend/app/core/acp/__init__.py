"""ACP (Active Context Pruning) framework-agnostic compression kernel.

The kernel is standalone: it depends only on the Python standard library
and never imports staffDeck business modules. The model (caller) writes
summaries; the kernel handles block management, checkpoints, retrieval,
nudging, and shadow-price accounting.
"""

from .blocks import Block, BlockStore
from .checkpoint import CheckpointRecord, CheckpointStore
from .config import AcpConfig
from .engine import AcpEngine, AcpError, CompressResult, DecompressResult, SearchResult
from .nudge import NudgeRecommendation
from .pricing import AcpLedger, LedgerEntry, TokenMeter
from .search import SearchHit, search_blocks
from .status import BlockLedgerEntry, CheckpointMappingEntry, ContextStatus
from .tiers import MAX_TIER, can_distill, next_tier, tier_label

__all__ = [
    "AcpConfig",
    "AcpEngine",
    "AcpError",
    "AcpLedger",
    "Block",
    "BlockLedgerEntry",
    "BlockStore",
    "CheckpointMappingEntry",
    "CheckpointRecord",
    "CheckpointStore",
    "CompressResult",
    "ContextStatus",
    "DecompressResult",
    "LedgerEntry",
    "MAX_TIER",
    "NudgeRecommendation",
    "SearchHit",
    "SearchResult",
    "TokenMeter",
    "can_distill",
    "next_tier",
    "search_blocks",
    "tier_label",
]